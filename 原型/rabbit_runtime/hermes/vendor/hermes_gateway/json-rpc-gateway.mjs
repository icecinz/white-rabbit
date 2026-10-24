// Generated from unchanged Hermes 145c713f1691b44bc485ce0ce4818cdb0e9c396b apps/shared/src/json-rpc-gateway.ts.
// SPDX-License-Identifier: MIT; see adjacent LICENSE.
export class JsonRpcGatewayError extends Error {
    code;
    data;
    constructor(message, options){
        super(message);
        this.name = 'JsonRpcGatewayError';
        this.code = options?.code;
        this.data = options?.data;
    }
}
const ANY = '*';
const DEFAULT_REQUEST_TIMEOUT_MS = 120_000;
const REPLAY_REQUEST_TIMEOUT_MS = 10_000;
const DEFAULT_HEARTBEAT_INTERVAL_MS = 15_000;
const DEFAULT_HEARTBEAT_DEADLINE_MS = 45_000;
const DEFAULT_CONNECT_TIMEOUT_MS = 15_000;
export function isGatewayWebSocketUrl(value) {
    if (typeof value !== 'string') {
        return false;
    }
    try {
        const protocol = new URL(value).protocol;
        return protocol === 'ws:' || protocol === 'wss:';
    } catch  {
        return false;
    }
}
export class JsonRpcGatewayClient {
    nextId = 0;
    pending = new Map();
    socket = null;
    state = 'idle';
    heartbeatTimer = null;
    heartbeatSequence = 0;
    lastInboundAt = 0;
    lastSeenSeq = new Map();
    replayInFlight = false;
    replayHold = null;
    replayEpoch = null;
    eventHandlers = new Map();
    stateHandlers = new Set();
    options;
    constructor(options = {}){
        this.options = {
            closedErrorMessage: options.closedErrorMessage ?? 'WebSocket closed',
            connectErrorMessage: options.connectErrorMessage ?? 'WebSocket connection failed',
            connectTimeoutMs: options.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT_MS,
            createRequestId: options.createRequestId ?? ((nextId)=>`${options.requestIdPrefix ?? 'r'}${nextId}`),
            heartbeatDeadlineMs: options.heartbeatDeadlineMs ?? DEFAULT_HEARTBEAT_DEADLINE_MS,
            heartbeatIntervalMs: options.heartbeatIntervalMs ?? DEFAULT_HEARTBEAT_INTERVAL_MS,
            notConnectedErrorMessage: options.notConnectedErrorMessage ?? 'gateway not connected',
            onSocketClose: options.onSocketClose ?? (()=>false),
            requestIdPrefix: options.requestIdPrefix ?? 'r',
            requestTimeoutMs: options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS,
            socketFactory: options.socketFactory
        };
    }
    get connectionState() {
        return this.state;
    }
    async connect(wsUrl) {
        const invalidUrl = ()=>{
            const got = typeof wsUrl === 'string' ? JSON.stringify(wsUrl) : `type "${typeof wsUrl}"`;
            return new Error(`gateway connect() requires a ws:// or wss:// URL string, got ${got}`);
        };
        if (!isGatewayWebSocketUrl(wsUrl)) {
            throw invalidUrl();
        }
        if (this.socket?.readyState === WebSocket.OPEN || this.state === 'connecting') {
            return;
        }
        this.setState('connecting');
        const socket = this.options.socketFactory?.(wsUrl) ?? new WebSocket(wsUrl);
        this.socket = socket;
        this.stopHeartbeat();
        socket.addEventListener('message', (message)=>{
            if (this.socket !== socket) {
                return;
            }
            this.lastInboundAt = Date.now();
            this.handleMessage(message.data);
        });
        socket.addEventListener('close', (event)=>{
            if (this.socket !== socket) {
                return;
            }
            if (this.options.onSocketClose(event)) {
                return;
            }
            this.socket = null;
            this.stopHeartbeat();
            this.setState('closed');
            this.rejectAllPending(new Error(this.options.closedErrorMessage));
        });
        await new Promise((resolve, reject)=>{
            let settled = false;
            let timer;
            const cleanup = ()=>{
                if (timer !== undefined) {
                    clearTimeout(timer);
                }
                socket.removeEventListener('open', onOpen);
                socket.removeEventListener('error', onError);
            };
            const onOpen = ()=>{
                if (settled || this.socket !== socket) {
                    return;
                }
                settled = true;
                cleanup();
                this.setState('open');
                resolve();
                void this.fetchReplay();
            };
            const onError = ()=>{
                if (settled || this.socket !== socket) {
                    return;
                }
                settled = true;
                cleanup();
                this.setState('error');
                reject(new Error(this.options.connectErrorMessage));
            };
            socket.addEventListener('open', onOpen, {
                once: true
            });
            socket.addEventListener('error', onError, {
                once: true
            });
            if (this.options.connectTimeoutMs > 0) {
                timer = setTimeout(()=>{
                    if (settled) {
                        return;
                    }
                    settled = true;
                    cleanup();
                    if (this.socket === socket) {
                        try {
                            socket.close();
                        } catch  {}
                        this.socket = null;
                        this.setState('error');
                    }
                    reject(new Error(this.options.connectErrorMessage));
                }, this.options.connectTimeoutMs);
            }
        });
    }
    close() {
        const socket = this.socket;
        if (!socket) {
            return;
        }
        try {
            socket.close();
        } finally{
            this.socket = null;
            this.stopHeartbeat();
            this.setState('closed');
            this.rejectAllPending(new Error(this.options.closedErrorMessage));
        }
    }
    invalidate(message = this.options.closedErrorMessage) {
        const socket = this.socket;
        if (!socket) {
            return;
        }
        this.invalidateSocket(socket, new Error(message));
    }
    on(type, handler) {
        let handlers = this.eventHandlers.get(type);
        if (!handlers) {
            handlers = new Set();
            this.eventHandlers.set(type, handlers);
        }
        handlers.add(handler);
        return ()=>handlers?.delete(handler);
    }
    onAny(handler) {
        return this.on(ANY, handler);
    }
    onEvent(handler) {
        return this.onAny(handler);
    }
    onState(handler) {
        this.stateHandlers.add(handler);
        handler(this.state);
        return ()=>this.stateHandlers.delete(handler);
    }
    request(method, params = {}, timeoutMs = this.options.requestTimeoutMs, signal) {
        const socket = this.socket;
        if (!socket || socket.readyState !== WebSocket.OPEN) {
            return Promise.reject(new Error(this.options.notConnectedErrorMessage));
        }
        if (signal?.aborted) {
            return Promise.reject(new DOMException('Aborted', 'AbortError'));
        }
        const id = this.options.createRequestId(++this.nextId);
        return new Promise((resolve, reject)=>{
            let onAbort;
            const detach = ()=>{
                if (onAbort && signal) {
                    signal.removeEventListener('abort', onAbort);
                }
            };
            const pending = {
                resolve: (value)=>{
                    detach();
                    resolve(value);
                },
                reject: (error)=>{
                    detach();
                    reject(error);
                }
            };
            if (timeoutMs > 0) {
                pending.timer = setTimeout(()=>{
                    if (this.pending.delete(id)) {
                        detach();
                        const seconds = Math.round(timeoutMs / 1000);
                        reject(new Error(`request timed out after ${seconds}s: ${method}`));
                    }
                }, timeoutMs);
            }
            if (signal) {
                onAbort = ()=>{
                    const call = this.pending.get(id);
                    if (call?.timer) {
                        clearTimeout(call.timer);
                    }
                    this.pending.delete(id);
                    detach();
                    reject(new DOMException('Aborted', 'AbortError'));
                };
                signal.addEventListener('abort', onAbort, {
                    once: true
                });
            }
            this.pending.set(id, pending);
            try {
                socket.send(JSON.stringify({
                    jsonrpc: '2.0',
                    id,
                    method,
                    params
                }));
            } catch (error) {
                this.clearPending(id);
                detach();
                reject(error instanceof Error ? error : new Error(String(error)));
            }
        });
    }
    handleMessage(raw) {
        const text = typeof raw === 'string' ? raw : String(raw);
        let frame;
        try {
            frame = JSON.parse(text);
        } catch  {
            return;
        }
        if (frame.id !== undefined && frame.id !== null) {
            const call = this.pending.get(frame.id);
            if (!call) {
                return;
            }
            this.clearPending(frame.id);
            if (frame.error) {
                call.reject(new JsonRpcGatewayError(frame.error.message || 'Hermes RPC failed', {
                    code: typeof frame.error.code === 'number' ? frame.error.code : undefined,
                    data: frame.error.data
                }));
            } else {
                call.resolve(frame.result);
            }
            return;
        }
        if (frame.method === 'event' && frame.params?.type) {
            if (frame.params.type === 'gateway.ready') {
                if (this.gatewayReadyAdvertisesHeartbeat(frame.params.payload)) {
                    const socket = this.socket;
                    if (socket) {
                        this.startHeartbeat(socket);
                    }
                }
                const epoch = frame.params.payload?.replay_epoch;
                if (typeof epoch === 'string' && epoch) {
                    this.adoptReplayEpoch(epoch);
                }
            }
            const sid = frame.params.session_id;
            const seqValue = frame.params.seq;
            if (this.replayHold && sid && typeof seqValue === 'number' && this.replayHold.has(sid)) {
                this.replayHold.get(sid)?.push(frame.params);
                return;
            }
            this.recordSeq(frame.params);
            this.dispatchEvent(frame.params);
        }
    }
    recordSeq(event) {
        const sid = event.session_id;
        const seq = event.seq;
        if (!sid || typeof seq !== 'number' || !Number.isFinite(seq)) {
            return;
        }
        const prev = this.lastSeenSeq.get(sid) ?? 0;
        if (seq > prev) {
            this.lastSeenSeq.set(sid, seq);
        }
    }
    getSeqWatermarks() {
        return Object.fromEntries(this.lastSeenSeq);
    }
    async fetchReplay() {
        if (this.replayInFlight || this.lastSeenSeq.size === 0) {
            return;
        }
        this.replayInFlight = true;
        const hold = new Map();
        for (const sid of this.lastSeenSeq.keys()){
            hold.set(sid, []);
        }
        this.replayHold = hold;
        try {
            const entries = Object.entries(this.getSeqWatermarks());
            const results = await Promise.allSettled(entries.map(([sid, lastSeen])=>this.request('session.events.since', {
                    session_id: sid,
                    last_seen: lastSeen
                }, REPLAY_REQUEST_TIMEOUT_MS)));
            for (const result of results){
                if (result.status !== 'fulfilled' || !Array.isArray(result.value?.events)) {
                    continue;
                }
                const epoch = result.value.epoch;
                if (typeof epoch === 'string' && epoch && this.replayEpoch && epoch !== this.replayEpoch) {
                    this.adoptReplayEpoch(epoch);
                    continue;
                }
                if (typeof epoch === 'string' && epoch && !this.replayEpoch) {
                    this.replayEpoch = epoch;
                }
                for (const event of result.value.events){
                    if (!event?.type) {
                        continue;
                    }
                    this.dispatchIfNewer(event);
                }
            }
        } catch  {} finally{
            this.flushReplayHold();
            this.replayInFlight = false;
        }
    }
    dispatchIfNewer(event) {
        const sid = event.session_id;
        const seq = event.seq;
        if (sid && typeof seq === 'number' && Number.isFinite(seq)) {
            const prev = this.lastSeenSeq.get(sid) ?? 0;
            if (seq <= prev) {
                return;
            }
            this.lastSeenSeq.set(sid, seq);
        }
        this.dispatchEvent(event);
    }
    adoptReplayEpoch(epoch) {
        if (this.replayEpoch === epoch) {
            return;
        }
        if (this.replayEpoch !== null) {
            this.lastSeenSeq.clear();
        }
        this.replayEpoch = epoch;
    }
    flushReplayHold() {
        const hold = this.replayHold;
        this.replayHold = null;
        if (!hold) {
            return;
        }
        for (const parked of hold.values()){
            for (const event of parked){
                this.dispatchIfNewer(event);
            }
        }
    }
    gatewayReadyAdvertisesHeartbeat(payload) {
        return Boolean(payload && typeof payload === 'object' && payload.heartbeat === true);
    }
    startHeartbeat(socket) {
        this.stopHeartbeat();
        this.lastInboundAt = Date.now();
        if (this.options.heartbeatIntervalMs <= 0 || this.options.heartbeatDeadlineMs <= 0) {
            return;
        }
        this.heartbeatTimer = setInterval(()=>{
            if (this.socket !== socket || socket.readyState !== WebSocket.OPEN) {
                return;
            }
            if (Date.now() - this.lastInboundAt >= this.options.heartbeatDeadlineMs) {
                this.invalidateSocket(socket, new Error('WebSocket heartbeat acknowledgement timed out'));
                return;
            }
            try {
                socket.send(JSON.stringify({
                    jsonrpc: '2.0',
                    id: `heartbeat-${++this.heartbeatSequence}`,
                    method: 'gateway.ping',
                    params: {}
                }));
            } catch (error) {
                this.invalidateSocket(socket, error instanceof Error ? error : new Error(String(error)));
            }
        }, this.options.heartbeatIntervalMs);
    }
    stopHeartbeat() {
        if (this.heartbeatTimer !== null) {
            clearInterval(this.heartbeatTimer);
            this.heartbeatTimer = null;
        }
    }
    invalidateSocket(socket, error) {
        if (this.socket !== socket) {
            return;
        }
        this.socket = null;
        this.stopHeartbeat();
        try {
            socket.close();
        } catch  {}
        this.setState('closed');
        this.rejectAllPending(error);
    }
    clearPending(id) {
        const call = this.pending.get(id);
        if (call?.timer) {
            clearTimeout(call.timer);
        }
        this.pending.delete(id);
    }
    dispatchEvent(event) {
        for (const handler of this.eventHandlers.get(event.type) ?? []){
            handler(event);
        }
        for (const handler of this.eventHandlers.get(ANY) ?? []){
            handler(event);
        }
    }
    rejectAllPending(error) {
        for (const [id, call] of this.pending){
            if (call.timer) {
                clearTimeout(call.timer);
            }
            call.reject(error);
            this.pending.delete(id);
        }
    }
    setState(state) {
        if (this.state === state) {
            return;
        }
        this.state = state;
        for (const handler of this.stateHandlers){
            handler(state);
        }
    }
}
