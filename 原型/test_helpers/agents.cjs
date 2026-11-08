'use strict';
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const web = path.join(__dirname, '..', 'rabbit_ui', 'assets');
function definitions() { return JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'agent_definitions.json'), 'utf8')); }
function load(sandbox) {
  sandbox.window.RabbitAgentDefinitions = definitions();
  vm.runInNewContext(fs.readFileSync(path.join(web, 'agents.js'), 'utf8'), sandbox);
  return sandbox.window.RabbitAgents;
}
module.exports = { definitions, load };
