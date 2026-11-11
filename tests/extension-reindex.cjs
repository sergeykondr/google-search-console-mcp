// Exercise the shipped content script's message handler and runJob function.
// Only the Google UI, timers and Chrome APIs are fakes; the skip decision is not.
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(process.argv[3]);
let listener;
let clicks = 0;
let resolve;
const finished = new Promise(r => { resolve = r; });
const context = vm.createContext({
  document: { body: { innerText: 'URL is on Google' } },
  window: { addEventListener() {}, postMessage() {} },
  chrome: {
    runtime: {
      onMessage: { addListener(fn) { listener = fn; } },
      connect() { return { onDisconnect: { addListener() {} } }; },
      sendMessage(msg) {
        if (msg.type === 'result') resolve({ outcome: msg.outcome, clicks });
      },
    },
    storage: { local: { get(_keys, cb) { cb({}); }, set() {} } },
  },
  setTimeout(fn) { queueMicrotask(fn); },
  setInterval() {},
  input,
  clicked() { clicks++; },
});
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
vm.runInContext(`
  inspect = async () => 'ok';
  accountMismatch = () => false;
  findClickable = () => ({});
  trustedClick = async () => clicked();
  awaitSubmission = async () => input.outcome;
`, context);
listener({ type: 'job', id: 'test', url: 'https://example.com/updated',
           authuser: '0', reindex: input.reindex }, {}, () => {});
finished.then(result => process.stdout.write(JSON.stringify(result)));
