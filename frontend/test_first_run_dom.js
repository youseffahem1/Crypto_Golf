/* Runs the REAL first-run guide and the REAL shared PIN dialog against the real
 * markup, in a DOM, with only the network stubbed. The static test checks the
 * source; this one proves the flow actually behaves:
 *
 *   new account (server says no PIN)  -> guide, step 1
 *   Next                             -> step 2, PIN explained
 *   Create my PIN                    -> the shared PIN prompt, no Cancel
 *   submit 1234                      -> POST /api/wallet/pin, guide closes
 *   return to the account (has PIN)   -> nothing is shown
 *
 * Usage: node test_first_run_dom.js <path to index.html>
 *   needs jsdom; point JSDOM_PATH at it if it is not on the default path.
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require(process.env.JSDOM_PATH || 'jsdom');

const file = process.argv[2] || path.join(__dirname, 'index.html');
let html = fs.readFileSync(file, 'utf8');

let fails = 0;
const ok = (label, cond, detail) => {
  if (cond) console.log('  ok   ' + label);
  else { fails++; console.log('  FAIL ' + label + (detail !== undefined ? ' -- ' + JSON.stringify(detail) : '')); }
};

/* Pull out just the two blocks this flow is made of, plus the dialogs' markup. */
const keep = [];
html = html.replace(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi, (m, attrs, body) => {
  const want = /id="vanta-security-signals-js"/.test(attrs) || /id="vanta-first-run-js"/.test(attrs);
  if (want) keep.push(body);
  return want ? m : '';
});
ok('found the shared PIN script', keep.some(b => b.includes('window.vantaRequirePin=')));
ok('found the first-run script', keep.some(b => b.includes('window.vantaOnboardMaybeShow=')));

/* A real origin is required, not the default about:blank: localStorage throws a
 * SecurityError on an opaque origin, and the PIN status path reads it. */
const ORIGIN = 'https://vanta.test/';
const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: ORIGIN });
const { window } = dom;
const $ = id => window.document.getElementById(id);
const openIds = () => ['vantaOnboard', 'vantaPinModal'].filter(id => $(id).classList.contains('open'));

/* ---- the network, and nothing else, is faked ------------------------------ */
let serverHasPin = false;
const calls = [];
window.accountMode = 'live';
window.vantaToken = () => 'test-token';
window.showToast = () => {};
window.vantaApi = (p, opts) => {
  calls.push([p, opts && opts.method || 'GET']);
  if (p === '/api/wallet/pin' && (!opts || opts.method !== 'POST')) {
    return Promise.resolve({ has_pin: serverHasPin });
  }
  if (p === '/api/wallet/pin' && opts.method === 'POST') {
    serverHasPin = true;                       /* the server now stores the hash */
    return Promise.resolve({ ok: true, has_pin: true });
  }
  return Promise.resolve({});
};
/* the guide must not depend on a canvas, but the PIN dialog must not crash */
window.HTMLCanvasElement.prototype.getContext = () => ({ fillStyle: '', clearRect() {}, fillRect() {} });
const copied = [];
window.navigator.clipboard = { writeText: t => { copied.push(t); return Promise.resolve(); } };

keep.forEach(b => window.eval(b));

const wait = ms => new Promise(r => setTimeout(r, ms));
const click = el => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));

(async () => {
  console.log('\n[1] a brand-new account is shown the guide');
  await window.vantaOnboardMaybeShow();
  await wait(30);
  ok('the server was asked whether this account has a PIN', calls.some(c => c[0] === '/api/wallet/pin' && c[1] === 'GET'));
  ok('the guide is open', $('vantaOnboard').classList.contains('open'), openIds());
  ok('it is on step one', $('vobStep1').className === 'vpm-step on' && $('vobStep2').className === 'vpm-step');
  ok('the instructions are the visible content', $('vobList').hidden === false);
  ok('the PIN note is not yet', $('vobNote').hidden === true);
  ok('the button says Next', $('vobNext').textContent === 'Next');
  ok('and the PIN dialog is NOT open yet', !$('vantaPinModal').classList.contains('open'));

  console.log('\n[2] Next explains that the PIN is required');
  click($('vobNext'));
  await wait(20);
  ok('still on the guide', $('vantaOnboard').classList.contains('open'));
  ok('step one is done and step two is current', $('vobStep1').className === 'vpm-step done' && $('vobStep2').className === 'vpm-step on');
  ok('the instructions are replaced by the PIN note', $('vobList').hidden === true && $('vobNote').hidden === false);
  ok('the button offers to create the PIN', $('vobNext').textContent === 'Create my PIN');
  ok('the title says what is happening', $('vobTitle').textContent === 'Protect your account');

  console.log('\n[3] it hands off to the shared PIN prompt');
  click($('vobNext'));
  await wait(40);
  ok('the PIN prompt is open', $('vantaPinModal').classList.contains('open'), openIds());
  ok('the guide is still behind it, not in front of it', $('vantaOnboard').classList.contains('open'));
  const z = id => {
    const m = new RegExp('#' + id + '\\{[^}]*z-index:(\\d+)').exec(html);
    return m ? +m[1] : 0;
  };
  ok('and it is ABOVE the guide in the stacking order, so it can be typed into',
    z('vantaPinModal') > z('vantaOnboard') && z('vantaOnboard') > 0,
    { guide: z('vantaOnboard'), pin: z('vantaPinModal') });
  ok('it is the create-PIN mode', $('vpinTitle').textContent === 'Create your account PIN');
  ok('it says the PIN is for every operation', /every operation/.test($('vpinText').textContent), $('vpinText').textContent);
  ok('Cancel is hidden, so it cannot be skipped', $('vpinCancel').style.display === 'none', $('vpinCancel').style.display);
  ok('the guide is still behind it', $('vantaOnboard').classList.contains('open'));

  console.log('\n[4] a bad PIN is refused, a good one is stored');
  $('vpinInput').value = '12';
  click($('vpinSubmit'));
  await wait(20);
  ok('a too-short PIN is rejected before any request', !calls.some(c => c[0] === '/api/wallet/pin' && c[1] === 'POST'));
  ok('and it is explained', /exactly 4 or 5 digits/.test($('vpinError').textContent), $('vpinError').textContent);

  $('vpinInput').value = '1234';
  click($('vpinSubmit'));
  await wait(60);
  ok('the PIN was sent to the server to be stored', calls.some(c => c[0] === '/api/wallet/pin' && c[1] === 'POST'));
  ok('the PIN prompt closed', !$('vantaPinModal').classList.contains('open'), openIds());
  ok('and the guide closed with it', !$('vantaOnboard').classList.contains('open'), openIds());
  ok('nothing was left in the clipboard', copied.length === 0);

  console.log('\n[5] a returning account is asked to CONFIRM, not to create again');
  /* This is the bug the server answer has to be truthful about: a session whose
     first protected action must not offer to overwrite a PIN that exists. */
  const back = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: ORIGIN });
  back.window.accountMode = 'live';
  back.window.vantaToken = () => 'test-token-back';
  back.window.showToast = () => {};
  const backCalls = [];
  back.window.vantaApi = (p, o) => {
    backCalls.push([p, o && o.method || 'GET']);
    if (p === '/api/wallet/pin' && o && o.method === 'POST') return Promise.reject(new Error('Enter your current PIN to change it.'));
    return Promise.resolve({ has_pin: true });
  };
  back.window.HTMLCanvasElement.prototype.getContext = () => ({ fillStyle: '', clearRect() {}, fillRect() {} });
  keep.forEach(b => back.window.eval(b));
  const status = await back.window.vantaPinStatus();
  ok('pinStatus reports the PIN that is really stored', status === true, status);
  /* And the case that actually bit users: the FIRST protected action of a
     session, before anything has been cached, must not offer to create a PIN
     that already exists. */
  const uncached = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: ORIGIN });
  uncached.window.accountMode = 'live';
  uncached.window.vantaToken = () => 'test-token-uncached';
  uncached.window.showToast = () => {};
  const pushed = [];
  uncached.window.vantaApi = (p, o) => {
    pushed.push([p, (o && o.method) || 'GET']);
    if (p === '/api/wallet/pin' && o && o.method === 'POST') return Promise.reject(new Error('Enter your current PIN to change it.'));
    return Promise.resolve({ has_pin: true });
  };
  uncached.window.HTMLCanvasElement.prototype.getContext = () => ({ fillStyle: '', clearRect() {}, fillRect() {} });
  keep.forEach(b => uncached.window.eval(b));
  uncached.window.vantaRequirePin(() => {});      /* nothing cached yet */
  await wait(60);
  const up = uncached.window.document;
  ok('with nothing cached, it still opens in CONFIRM mode',
    up.getElementById('vpinTitle').textContent === 'Confirm Wallet PIN', up.getElementById('vpinTitle').textContent);
  ok('so a returning account is never asked to create a PIN over the old one',
    !pushed.some(c => c[0] === '/api/wallet/pin' && c[1] === 'POST'), pushed);
  uncached.window.close();

  back.window.vantaRequirePin(() => {});
  await wait(40);
  const bp = back.window.document.getElementById('vantaPinModal');
  ok('the prompt is in confirm mode, not create mode',
    back.window.document.getElementById('vpinTitle').textContent === 'Confirm Wallet PIN',
    back.window.document.getElementById('vpinTitle').textContent);
  ok('so Cancel is offered again', back.window.document.getElementById('vpinCancel').style.display !== 'none');
  ok('and no PIN was pushed at the server', !backCalls.some(c => c[0] === '/api/wallet/pin' && c[1] === 'POST'), backCalls);
  back.window.close();

  console.log('\n[6] the account is never asked a second time');
  await window.vantaOnboardMaybeShow();
  await wait(30);
  ok('returning to the same account shows nothing', openIds().length === 0, openIds());
  ok('even when re-checked, the server now reports a PIN', serverHasPin === true);

  console.log('\n[7] a returning account with a PIN is never shown the guide');
  serverHasPin = true;
  const fresh = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: ORIGIN });
  fresh.window.accountMode = 'live';
  fresh.window.vantaToken = () => 'test-token-2';
  fresh.window.showToast = () => {};
  fresh.window.vantaApi = p => Promise.resolve({ has_pin: true });
  fresh.window.HTMLCanvasElement.prototype.getContext = () => ({ fillStyle: '', clearRect() {}, fillRect() {} });
  keep.forEach(b => fresh.window.eval(b));
  await fresh.window.vantaOnboardMaybeShow();
  await wait(30);
  const g = fresh.window.document.getElementById('vantaOnboard');
  ok('the guide stays shut for an account that already has a PIN', !g.classList.contains('open'));
  fresh.window.close();

  console.log('\n[8] a guest with no account is left alone');
  const guest = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: ORIGIN });
  guest.window.accountMode = 'demo';
  guest.window.vantaToken = () => '';             /* not signed in */
  guest.window.showToast = () => {};
  let guestCalls = 0;
  guest.window.vantaApi = () => { guestCalls++; return Promise.resolve({ has_pin: false }); };
  guest.window.HTMLCanvasElement.prototype.getContext = () => ({ fillStyle: '', clearRect() {}, fillRect() {} });
  keep.forEach(b => guest.window.eval(b));
  await guest.window.vantaOnboardMaybeShow();
  await wait(30);
  ok('no guide, and no pointless request', !guest.window.document.getElementById('vantaOnboard').classList.contains('open') && guestCalls === 0, { guestCalls });
  guest.window.close();

  window.close();
  console.log();
  if (fails) { console.log('FAILED (' + fails + ')'); process.exit(1); }
  console.log('All first-run guide DOM checks passed.');
  process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
