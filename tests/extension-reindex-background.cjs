const fs = require('node:fs');
const vm = require('node:vm');
const supported = JSON.parse(process.argv[3]);
const jobs = [];
const event = { addListener() {} };
const context = vm.createContext({
  chrome: {
    runtime: {
      getManifest() { return { version: 'test' }; },
      onStartup: event, onInstalled: event, onMessage: event, onConnect: event,
    },
    storage: { local: { get(_keys, cb) { cb({ paused: true }); }, set() {} } },
    alarms: { create() {}, onAlarm: event },
    debugger: { onDetach: event },
    tabs: {
      onRemoved: event,
      async sendMessage(id, message) {
        if (message.type === 'capabilities') return supported ? { reindex: true } : {};
        jobs.push(message);
        return { accepted: true };
      },
    },
  },
  setTimeout() { throw Error('unexpected real wait'); },
});
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
vm.runInContext('ensureDebugger = async () => true;', context);
vm.runInContext(`dispatchJob(1, {id: 'test', url: 'https://example.com/updated',
                               authuser: '0', reindex: true}, 1)`, context)
  .then(accepted => process.stdout.write(JSON.stringify({ accepted, jobs })));
