/* Drives the real "Move to Wallet" flow out of frontend/index.html: PROFIT is
   rendered, the source of the profit is listed, the button opens the dialog, a
   coin is chosen, a PIN is typed, and only then is a request made. The code
   under test is sliced straight out of the file, so this cannot drift from the
   implementation.

   What is stubbed is the SERVER, not the page: /api/platform/coins and
   /api/trade/history answer with the same shapes the backend returns (verified
   by backend/smoke_profit_pin.py), and /api/platform/move-profit records the
   request instead of moving money. That lets this test prove the page sends the
   right thing and refuses a wrong PIN without a live account.

   Run: node test_profit_move_flow.js <path-to-index.html>                     */
const fs = require('fs');
const vm = require('vm');

const file = process.argv[2];
const html = fs.readFileSync(file, 'utf8');
const lines = html.split(/\r?\n/);

function slice(fromMarker, toMarker) {
  const a = lines.findIndex(l => l.includes(fromMarker));
  if (a < 0) throw new Error('start marker not found: ' + fromMarker);
  let b = -1;
  for (let i = a + 1; i < lines.length; i++) {
    if (toMarker && lines[i].includes(toMarker)) { b = i; break; }
  }
  if (b < 0) throw new Error('end marker not found: ' + toMarker);
  return lines.slice(a, b).join('\n');
}

/* The dashboard-cards block: mainEl, byId, usd, setText, setNeg, activeSymbol,
   liveSplit, setMoveAvailability, writeResult and the profit-source renderer. */
const renderSrc = slice('if (window.__vantaDashboardCards) return;', 'function refreshInfoCards(){');
/* The two-step Move dialog, its helpers and its wiring. */
const moveSrc = slice('"Move to Wallet": realized trading profit',
  '/* ---- Dashboard balance card quick actions (delegated) ---- */');
/* The cards' button wiring, which includes the profit-source disclosure toggle.
   It lives below refreshInfoCards, so it needs its own slice. Started a little
   earlier than `wire()` itself so the scroll helper it calls is in scope. */
const wireSrc = slice('function scrollToTrade(){', 'setInterval(function(){');

const FAILS = [];
function check(label, got, want) {
  if (got !== want) { FAILS.push(label + ': got ' + got + ' want ' + want); console.log('  FAIL ' + label + ': got ' + got + ' want ' + want); }
  else console.log('  ok   ' + label + ' = ' + got);
}
function ok(label, cond) { check(label, !!cond, true); }

/* ---- a DOM stub with just enough behaviour for the real code ------------- */
function El(id) {
  const handlers = {};
  return {
    id, textContent: '', innerHTML: '', value: '', className: '', hidden: false,
    disabled: false, title: '', type: '', maxlength: '', placeholder: '',
    dataset: {}, attrs: {}, classes: new Set(), coins: [],
    classList: {
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
      toggle(c, on) { const want = on === undefined ? !this._s.has(c) : !!on; if (want) this._s.add(c); else this._s.delete(c); },
      _s: null,
    },
    addEventListener(ev, fn) { (handlers[ev] = handlers[ev] || []).push(fn); },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; },
    querySelectorAll(sel) {
      if (sel === '.vpm-coin') return this.coins;
      return [];
    },
    querySelector() { return null; },
    closest() { return null; },
    focus() {},
    blur() {},
    _fire(ev, e) { (handlers[ev] || []).forEach(fn => fn(e || { target: this, preventDefault() {} })); },
    _has(ev) { return (handlers[ev] || []).length > 0; },
  };
}

const els = {};
for (const id of ['icProfit', 'icLoss', 'icProfitSub', 'icMoveBtn', 'icProfitSrc', 'icProfitSrcList',
  'vantaProfitMove', 'vpmTitle', 'vpmSub', 'vpmStep1', 'vpmStep2', 'vpmCoinStep', 'vpmPinStep',
  'vpmCoins', 'vpmSrc', 'vpmAmount', 'vpmTo', 'vpmDots', 'vpmPin', 'vpmError',
  'vpmNext', 'vpmBack', 'vpmCancel']) els[id] = El(id);
/* classList needs its backing set bound to the element it belongs to. */
for (const id in els) els[id].classList._s = els[id].classes;

const DOTS = [El('d0'), El('d1'), El('d2'), El('d3'), El('d4')];
DOTS.forEach(d => { d.classList._s = d.classes; });
const COINS = [
  { symbol: 'USDT', name: 'Tether', price: 1, tradeable: true },
  { symbol: 'GOLF', name: 'Golf Coin', price: 2, tradeable: true },
  { symbol: 'BTC', name: 'Bitcoin', price: 50000, tradeable: true },
  { symbol: 'DOGE2', name: 'Untradeable', price: 0, tradeable: false },
];
const HISTORY = [
  { id: 't-1', symbol: 'GOLF', direction: 'UP', amount: 100, profit: 20, status: 'WON', settled_at: '2026-09-27T10:00:00Z', profit_moved: false },
  { id: 't-2', symbol: 'GOLF', direction: 'UP', amount: 50, profit: 5, status: 'WON', settled_at: '2026-09-27T11:00:00Z', profit_moved: true },
  { id: 't-3', symbol: 'GOLF', direction: 'DOWN', amount: 50, profit: -10, status: 'LOST', settled_at: '2026-09-27T12:00:00Z' },
  { id: 't-4', symbol: 'BTC', direction: 'UP', amount: 1, profit: 99, status: 'WON', settled_at: '2026-09-27T13:00:00Z', profit_moved: false },
];
const TOASTS = [];
const REQUESTS = [];
let PIN_STATE = { has_pin: true };
/* The move endpoint's answer, and the failure it should give for a bad PIN. */
let MOVE_BEHAVIOUR = { status: 200, body: { symbol: 'GOLF', dest_symbol: 'GOLF', usd_moved: 20, coin_amount: 10, price: 2 } };

function apiStub(path, opts) {
  REQUESTS.push({ path, method: (opts && opts.method) || 'GET', body: opts && opts.body });
  if (path === '/api/platform/coins') return Promise.resolve({ coins: COINS });
  if (path === '/api/trade/history') return Promise.resolve(HISTORY);
  if (path === '/api/wallet/pin') {
    if (opts && opts.method === 'POST') { PIN_STATE = { has_pin: true }; return Promise.resolve({ ok: true, has_pin: true }); }
    return Promise.resolve(PIN_STATE);
  }
  if (path === '/api/platform/move-profit') {
    if (MOVE_BEHAVIOUR.status !== 200) {
      return Promise.reject(new Error(MOVE_BEHAVIOUR.detail));
    }
    return Promise.resolve(MOVE_BEHAVIOUR.body);
  }
  return Promise.resolve(null);
}
const tick = () => new Promise(r => setImmediate(r));

const sandbox = {
  Math, Number, String, Array, Object, JSON, Boolean, isFinite, Date, Promise, RegExp, Error, parseInt, parseFloat,
  setTimeout: (fn) => { try { fn(); } catch (e) { /* page timers are not under test */ } },
  clearTimeout() {},
  requestAnimationFrame: (fn) => { if (typeof fn === 'function') fn(); return 0; },
  console,
  document: {
    getElementById: id => els[id] || null,
    querySelector: () => null,
    querySelectorAll: sel => (sel === '#vpmDots .vpin-dot' ? DOTS : []),
    addEventListener() {},
  },
  localStorage: { _d: {}, getItem(k) { return this._d[k] === undefined ? null : this._d[k]; }, setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; } },
  accountMode: 'live',
  vantaApi: apiStub,
  /* The real page exposes this from its PIN dialog script. The move dialog only
     needs the contract, so the stub answers it from the same PIN state the
     /api/wallet/pin stub uses. */
  vantaPinStatus: () => Promise.resolve(PIN_STATE.has_pin),
  vantaToken: () => 'test-token',
  vantaPlatformPrice: (s) => (COINS.find(c => c.symbol === s && c.tradeable) || { price: 0 }).price,
  vantaActiveCoin: 'GOLF',
  esc: s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
  toast: m => TOASTS.push(m),
  showToast: m => TOASTS.push(m),
  /* UI refresh hooks: record that the page tried, without needing a real page. */
  vantaRefreshInfoCards: () => {},
  vantaRefreshWalletSplit: () => {},
  vantaRefreshDashboard: () => {},
  vantaSyncWalletUI: () => {},
  vantaSyncPosCard: () => {},
  updateBalance: () => {},
  refreshBalance: () => {},
  icSrcInvalidate: () => {},
};
sandbox.window = sandbox;
sandbox.window.vantaCoinBalances = null;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(
  'var __out={};\n(function(){\n' + renderSrc + '\n' + moveSrc + '\n' + wireSrc + '\n' +
  '  __out.writeResult=writeResult;__out.setMoveAvailability=setMoveAvailability;\n' +
  '  __out.dest=function(){return vpmDest;};__out.sym=function(){return vpmSym;};\n' +
  '  __out.open=window.vantaMoveProfitToWallet;return __out;\n})()',
  sandbox
);
const page = sandbox.__out || vm.runInContext('({open:window.vantaMoveProfitToWallet})', sandbox);
const fire = (id, ev, e) => els[id]._fire(ev, e);
const modalOpen = () => els.vantaProfitMove.classes.has('open');
const moveReqs = () => REQUESTS.filter(r => r.path === '/api/platform/move-profit');

(async function run() {

  console.log('[1] the card renders PROFIT with no "+" and lists where it came from');
  page.writeResult({ profit: 25, loss: -10, available: 20 }, 'GOLF');
  check('PROFIT has no plus sign', els.icProfit.textContent, '$25.00');
  ok('and never a minus either', !/[-+]\$/.test(els.icProfit.textContent));
  check('LOSS still shows its own sign', els.icLoss.textContent, '\u2212$10.00');
  await tick(); await tick();
  ok('the source disclosure is shown', els.icProfitSrc.hidden === false);
  /* Two GOLF winners came back (t-1 +$20, t-2 +$5, t-2 already moved). The
     GOLF loser (t-3) and the BTC trade (t-4) are not this card's profit. */
  const label = els.icProfitSrc.innerHTML;
  check('the summary counts only this coin\'s winning trades',
    /2 winning trades/.test(label), true);
  ok('and says how much is still to move', /1 still to move/.test(label));
  const src = els.icProfitSrcList.innerHTML;
  ok('the first winning trade is listed', /t-1/.test(src) && /\$20\.00/.test(src));
  ok('the already-moved one is listed too', /t-2/.test(src) && /\$5\.00/.test(src));
  ok('the already-moved one is marked as moved', /moved/.test(src));
  ok('the losing trade is not offered as profit', !/t-3/.test(src));
  ok('another coin\'s trade is not mixed in', !/t-4/.test(src));
  ok('the untradeable coin is not offered', !/DOGE2/.test(els.vpmCoins.innerHTML));

  console.log('\n[1b] the source list is a disclosure, not always-on clutter');
  /* Seed the stub from the real markup so the initial state under test is the
     one the page actually ships, not a guess made here. */
  const shipped = html.match(/id="icProfitSrc"[^>]*aria-expanded="(\w+)"/);
  check('the markup ships it collapsed', shipped ? shipped[1] : null, 'false');
  els.icProfitSrc.setAttribute('aria-expanded', shipped ? shipped[1] : 'false');
  check('it starts collapsed', els.icProfitSrc.getAttribute('aria-expanded'), 'false');
  check('and the rows are hidden', els.icProfitSrcList.hidden, true);
  fire('icProfitSrc', 'click');
  check('clicking expands it', els.icProfitSrc.getAttribute('aria-expanded'), 'true');
  check('and reveals the rows', els.icProfitSrcList.hidden, false);
  fire('icProfitSrc', 'click');
  check('clicking again collapses it', els.icProfitSrc.getAttribute('aria-expanded'), 'false');
  check('and hides them again', els.icProfitSrcList.hidden, true);

  console.log('\n[2] clicking Move opens the dialog on the COIN step');
  check('the button is enabled', els.icMoveBtn.disabled, false);
  page.open();
  await tick(); await tick();
  check('the dialog is open', modalOpen(), true);
  check('the coin step is showing', els.vpmCoinStep.hidden, false);
  check('the PIN step is not yet', els.vpmPinStep.hidden, true);
  check('the next button says Continue', els.vpmNext.textContent, 'Continue');
  ok('real coins are offered', /BTC/.test(els.vpmCoins.innerHTML) && /GOLF/.test(els.vpmCoins.innerHTML));
  ok('untradeable coins are not offered', !/DOGE2/.test(els.vpmCoins.innerHTML));
  check('nothing has been sent yet', moveReqs().length, 0);

  console.log('\n[3] choosing a coin moves to the PIN step without sending anything');
  const btc = El('btc'); btc.dataset.sym = 'BTC';
  els.vpmCoins.coins = [btc];
  fire('vpmCoins', 'click', { target: { closest: () => btc }, preventDefault() {} });
  fire('vpmNext', 'click');
  check('the chosen coin is the one the page will use', page.dest(), 'BTC');
  check('the PIN step is showing', els.vpmPinStep.hidden, false);
  check('the coin step is gone', els.vpmCoinStep.hidden, true);
  check('a back button is offered', els.vpmBack.hidden, false);
  check('the amount is the available profit', els.vpmAmount.textContent, '$20.00');
  ok('the preview names the chosen coin', /BTC/.test(els.vpmTo.textContent));
  ok('and the price it used', /50,000|\$50000/.test(els.vpmTo.textContent));
  check('still nothing has been sent', moveReqs().length, 0);

  console.log('\n[4] a WRONG PIN is refused and moves nothing');
  MOVE_BEHAVIOUR = { status: 400, detail: 'Invalid PIN.' };
  els.vpmPin.value = '9999';
  fire('vpmNext', 'click');
  await tick(); await tick();
  check('the PIN step stays open', els.vpmPinStep.hidden, false);
  check('the error is shown in the dialog', /Incorrect PIN/.test(els.vpmError.textContent), true);
  check('the wrong PIN was sent for checking', moveReqs().length, 1);
  check('no success message was shown', TOASTS.length, 0);
  check('the dialog was not closed', modalOpen(), true);

  console.log('\n[5] a lockout is reported as such, not as a wrong PIN');
  MOVE_BEHAVIOUR = { status: 400, detail: 'Too many incorrect PIN attempts. Try again in 42s.' };
  els.vpmPin.value = '9999';
  fire('vpmNext', 'click');
  await tick(); await tick();
  check('the lockout message reaches the user', /Too many/.test(els.vpmError.textContent), true);
  check('the PIN is not described as merely wrong', /Incorrect PIN/.test(els.vpmError.textContent), false);

  console.log('\n[6] the CORRECT PIN moves the profit into the chosen coin');
  MOVE_BEHAVIOUR = { status: 200, body: { symbol: 'GOLF', dest_symbol: 'BTC', usd_moved: 20, coin_amount: 0.0004, price: 50000 } };
  els.vpmPin.value = '4321';
  fire('vpmNext', 'click');
  await tick(); await tick();
  const req = moveReqs()[moveReqs().length - 1];
  check('it posted to the profit endpoint', req.path, '/api/platform/move-profit');
  check('it named the traded coin', req.body.symbol, 'GOLF');
  check('it named the CHOSEN destination', req.body.dest_symbol, 'BTC');
  check('it sent the PIN', req.body.pin, '4321');
  ok('it sent NO amount of its own', !('amount' in req.body) && !('usd_moved' in req.body));
  check('the dialog closed', modalOpen(), false);
  ok('the user is told what landed', /GOLF profit moved to your BTC wallet/.test(TOASTS[TOASTS.length - 1]));
  ok('and the real amount is quoted, not the estimate', /0\.0004/.test(TOASTS[TOASTS.length - 1]));

  console.log('\n[7] an account with no PIN yet is offered the create step first');
  PIN_STATE = { has_pin: false };
  els.icMoveBtn.dataset.vantaAvail = '20';
  page.open();
  await tick(); await tick();
  fire('vpmNext', 'click');
  check('it asks to create a PIN', /Create your wallet PIN/.test(els.vpmTitle.textContent), true);
  check('the button says it will create and move', /Create PIN & move/.test(els.vpmNext.textContent), true);
  els.vpmPin.value = '4321';
  MOVE_BEHAVIOUR = { status: 200, body: { symbol: 'GOLF', dest_symbol: 'GOLF', usd_moved: 20, coin_amount: 10, price: 2 } };
  fire('vpmNext', 'click');
  await tick(); await tick();
  ok('a PIN was stored before the move',
    REQUESTS.some(r => r.path === '/api/wallet/pin' && r.method === 'POST' && r.body.pin === '4321'));
  ok('then the profit moved', moveReqs().length > 0);

  console.log('\n[8] nothing is moveable when there is no positive profit');
  page.writeResult({ profit: 0, loss: -60, available: 0 }, 'GOLF');
  check('the button is disabled', els.icMoveBtn.disabled, true);
  const before = moveReqs().length;
  page.open();
  await tick();
  check('opening it moved nothing', moveReqs().length, before);
  check('and it never opened', modalOpen(), false);

  console.log('\n' + '='.repeat(60));
  if (FAILS.length) {
    console.log(FAILS.length + ' CHECK(S) FAILED:');
    FAILS.forEach(f => console.log('  - ' + f));
    process.exit(1);
  }
  console.log('ALL CHECKS PASSED');
})().catch(e => { console.error('ERROR: ' + e.message + '\n' + e.stack); process.exit(1); });
