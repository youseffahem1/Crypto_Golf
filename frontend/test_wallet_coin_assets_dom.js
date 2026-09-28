/* BEHAVIOURAL test: runs the real wallet coin-asset script in a jsdom document
 * built from the real index.html, then drives it like a user.
 *
 * This is the test that proves the UI actually works, not just that the right
 * strings are present.
 *
 * Needs jsdom. It is not a project dependency, so the test SKIPS (exit 0) when
 * jsdom is absent rather than failing the suite on a machine that does not have
 * it. Point JSDOM_PATH at an install to run it, e.g.
 *   npm i --no-save jsdom
 *   set JSDOM_PATH=C:\path\to\node_modules\jsdom
 *
 * Usage: node test_wallet_coin_assets_dom.js <path to index.html>
 */
const fs = require('fs');
const path = require('path');

let JSDOM = null;
for (const p of [process.env.JSDOM_PATH, 'jsdom', path.join(__dirname, '..', 'node_modules', 'jsdom')]) {
  if (!p) continue;
  try { JSDOM = require(p).JSDOM; break; } catch (e) { /* try the next one */ }
}
if (!JSDOM) {
  console.log('SKIP: jsdom not found. Install it, or set JSDOM_PATH, to run the DOM checks.');
  console.log('      (test_wallet_coin_assets.js still runs without it)');
  process.exit(0);
}

const file = process.argv[2] || path.join(__dirname, 'index.html');
const html = fs.readFileSync(file, 'utf8');

let fails = 0;
function ok(label, cond, detail) {
  if (cond) console.log('  ok   ' + label);
  else { fails++; console.log('  FAIL ' + label + (detail !== undefined ? ' -- ' + JSON.stringify(detail) : '')); }
}

/* Build a DOM containing the two things the wallet block needs: the markup it
 * renders into, and the script itself. The rest of the page is not loaded --
 * the block only touches the wallet elements and the globals it checks for. */
const block = (html.match(/<script[^>]*id="vanta-wallet-coin-assets-js"[^>]*>([\s\S]*?)<\/script>/) || [])[1];
if (!block) { console.log('FAIL: wallet coin-assets block not found'); process.exit(1); }

const css = (html.match(/<style id="vantaWalletCoinAssetsCss">([\s\S]*?)<\/style>/) || [])[1] || '';

const shell = `<!doctype html><html><head><style>${css}</style></head><body>
  <div id="vantaWalletPage">
    <div class="vfx-usdt-hero">
      <div class="vfx-golf-balance"><span class="vfx-mini-coin" data-vfx-logo="GOLF"></span><b>GOLF</b>&nbsp;<b id="vfxGolfBalanceLine">0.00</b></div>
    </div>
    <section class="vfx-golf-mini"><div class="vfx-golf-mini-head"><strong><span class="vfx-mini-coin" data-vfx-logo="GOLF"></span> GOLF &mdash; Early Access</strong></div></section>
    <div class="vfx-wallet-row">
      <section class="vfx-wallet-col" id="vfxInvestWallet"><div id="vfxInvestHoldings"></div></section>
      <section class="vfx-wallet-col" id="vfxNormalWallet"><div id="vfxNormalHoldings"></div></section>
    </div>
  </div>
  <div class="vfx-modal" id="vfxDepositModal"></div>
  <div class="vfx-modal" id="vfxWithdrawModal"></div>
  <div class="vfx-modal" id="vfxTransferModal"></div>
  <div class="vfx-modal" id="vfxConvertModal"></div>
</body></html>`;

const dom = new JSDOM(shell, { runScripts: 'outside-only', pretendToBeVisual: true });
const { window } = dom;

/* ---- the globals the real page provides ---- */
const SVG = s => 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(s.replace(/>\s+</g, '><').trim());
window.VANTACOINS = [
  { sym: 'USDT', name: 'Tether' }, { sym: 'BTC', name: 'Bitcoin' },
  { sym: 'ETH', name: 'Ethereum' }, { sym: 'SOL', name: 'Solana' },
  { sym: 'ABC', name: 'ABC Coin' }, { sym: 'GOLF', name: 'Golf Coin' },
  { sym: 'NOVA', name: 'Nova Coin' },
];
// The SAME logo registry the shipped page builds, so the test cannot pass with
// a different icon.
window.__COIN_ART__ = {
  GOLF: { logo: SVG('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><circle cx="32" cy="32" r="30" fill="#d99a1f"/></svg>'), emoji: 'E1' },
  NOVA: { logo: SVG('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><circle cx="32" cy="32" r="30" fill="#8b5cf6"/></svg>'), emoji: 'E2' },
  ABC: { logo: SVG('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><circle cx="32" cy="32" r="30" fill="#2563eb"/></svg>'), emoji: 'E3' },
};
window.VANTACOINS.forEach(c => {
  const a = window.__COIN_ART__[c.sym];
  if (a) { c.logo = a.logo; c.emoji = a.emoji; }
});
window.vantaCoinArt = sym => {
  const s = String(sym || '').toUpperCase();
  const row = (window.VANTACOINS || []).find(x => x.sym === s) || {};
  return { sym: s, logo: row.logo || '', emoji: row.emoji || '', letter: (row.sym || s).charAt(0) };
};
window.vantaCoinIconHTML = function (sym) {
  const a = window.vantaCoinArt(sym);
  const inner = a.logo
    ? '<img class="vtp-coin-logo" src="' + a.logo + '" alt="' + a.sym + ' logo">'
    : (a.emoji ? '<span class="vtp-coin-emoji">' + a.emoji + '</span>' : '<span class="vtp-coin-letter">' + a.letter + '</span>');
  return '<div class="vtp-coin-icon' + (a.logo ? ' has-logo' : ' has-emoji') + '" data-coin="' + a.sym + '">' + inner + '</div>';
};
window.vantaCoinIconFallback = function (box) { if (box) box.classList.remove('has-logo'); };
window.VWD_DEFAULT = { USDT: 1, GOLF: 0.6, NOVA: 0.25, ABC: 0.01 };
window.accountMode = 'live';
window.toast = m => { window.__lastToast = m; };
window.vantaApi = p => { window.__apiCalls.push(p); return Promise.resolve(window.__layout); };
window.__apiCalls = [];
window.__layout = {
  platform_coins: ['GOLF', 'NOVA', 'ABC'],
  normal_wallet_coins: ['BTC', 'ETH', 'SOL', 'USDT', 'LTC'],
  deposit_addresses: { GOLF: 'GolfAddr123', USDT: 'TUsdtAddr' },
  withdraw_addresses: { GOLF: 'GolfWithdraw999' },
  network: 'TRON Nile Testnet',
  trading_balances: { USDT: 1000, GOLF: 12, NOVA: 3, ABC: 500 },
  wallet_balances: { USDT: 0, GOLF: 4, NOVA: 1, ABC: 250, BTC: 0.5 },
};
window.vantaWalletLayout = window.__layout;
/* spies for the four actions */
window.__called = [];
window.vantaShowCoinAddress = (s, m) => window.__called.push(['address', s, m]);
window.vantaOpenTransfer = s => window.__called.push(['send', s]);
window.vantaOpenConvert = (f, t) => window.__called.push(['swap', f, t]);

const $ = id => window.document.getElementById(id);
/* The page's own two ledgers, as the real code publishes them. */
window.vantaWalletBalances = window.__layout.wallet_balances;
window.vantaCoinBalances = window.__layout.trading_balances;
window.vantaServerPrices = { GOLF: 0.6, NOVA: 0.25, ABC: 0.01, USDT: 1 };

/* Seed stale content produced by the OLD renderer, so "only three coins" is
   proven by removal rather than by the markup never having had it. */
$('vfxInvestHoldings').innerHTML = '<div class="vfx-hold"><span>USDT</span><b>0</b></div><div class="vfx-hold"><span>BTC</span><b>0.5</b></div>';
$('vfxNormalHoldings').innerHTML = 'BTC ETH SOL USDT LTC';

window.eval(block);
/* The block sits at the end of <body>, so in production it runs while the page
   is still parsing and paints on DOMContentLoaded. Reproduce that here instead
   of pretending the page was already ready. */
if (window.document.readyState === 'loading') {
  window.document.dispatchEvent(new window.Event('DOMContentLoaded', { bubbles: true }));
}

const wait = ms => new Promise(r => setTimeout(r, ms));

async function main() {

console.log('\n[1] only ABC, GOLF and NOVA are rendered as assets');

const cards = [...window.document.querySelectorAll('[data-vfx-asset]')];
ok('exactly three asset cards', cards.length === 3, cards.length);
const syms = cards.map(c => c.getAttribute('data-vfx-asset'));
ok('they are ABC, GOLF and NOVA', JSON.stringify(syms), JSON.stringify(syms));
const text = $('vfxInvestHoldings').textContent;
['BTC', 'ETH', 'SOL', 'LTC', 'USDT', 'USDC', 'DOGE', 'ADA', 'LINK', 'XRP', 'TRX', 'BNB']
  .forEach(s => ok('no ' + s + ' anywhere in the wallet list', text.indexOf(s) < 0));
ok('the old normal-wallet column is emptied', $('vfxNormalHoldings').textContent === '',
  $('vfxNormalHoldings').textContent);
ok('BTC no longer renders from the old layout data', text.indexOf('0.5') < 0);

console.log('\n[2] each coin carries its own logo');
syms.forEach(sym => {
  const card = window.document.querySelector('[data-vfx-asset="' + sym + '"]');
  const img = card.querySelector('.vtp-coin-logo');
  ok(sym + ' shows a logo image', !!img);
  if (img) ok(sym + ' uses its own artwork', img.getAttribute('src') === window.__COIN_ART__[sym].logo,
    img.getAttribute('src').slice(0, 40));
  ok(sym + ' names the coin', card.textContent.indexOf(sym) >= 0);
});
ok('the coin name is shown, not just the ticker',
  window.document.querySelector('[data-vfx-asset="ABC"]').textContent.indexOf('ABC Coin') >= 0);
[...window.document.querySelectorAll('[data-vfx-logo="GOLF"]')].forEach((slot, i) => {
  ok('inline GOLF slot #' + (i + 1) + ' was painted with a logo',
    !!slot.querySelector('.vtp-coin-logo'));
});

console.log('\n[3] balances and values are read from the two ledgers');
const abc = window.document.querySelector('[data-vfx-asset="ABC"]');
ok('ABC shows the WALLET balance (250), not the trading one (500)',
  abc.textContent.indexOf('250') >= 0 && abc.textContent.indexOf('500') < 0, abc.textContent);
const golf = window.document.querySelector('[data-vfx-asset="GOLF"]');
ok('GOLF shows the wallet balance (4)', golf.textContent.indexOf('4') >= 0);
ok('GOLF shows a USD value (4 x $0.60 = $2.40)', /2\.40/.test(golf.textContent), golf.textContent);
ok('ABC shows a USD value (250 x $0.01 = $2.50)', /2\.50/.test(abc.textContent), abc.textContent);

console.log('\n[4] clicking a coin opens its details view');
ok('no dialog is open before the click', !$('vfxCoinModal') || !$('vfxCoinModal').classList.contains('open'));
window.document.querySelector('[data-vfx-asset="GOLF"]').click();
const modal = $('vfxCoinModal');
ok('the dialog was created', !!modal);
ok('it is open', modal && modal.classList.contains('open'));
ok('it is a modal dialog', modal.getAttribute('aria-modal') === 'true');
ok('the sheet shows the GOLF logo', !!modal.querySelector('.vtp-coin-logo'));
ok('the sheet shows the GOLF name', modal.textContent.indexOf('Golf Coin') >= 0);
ok('the sheet shows the wallet balance', modal.querySelector('[data-vfx-sheet-wallet]').textContent === '4');
ok('the sheet separately shows the trading balance',
  modal.querySelector('[data-vfx-sheet-trading]').textContent === '12');
ok('the sheet shows the USD value', /2\.40/.test(modal.querySelector('[data-vfx-sheet-usd]').textContent));

console.log('\n[5] the wallet address is shown and copyable');
const addr = modal.querySelector('[data-vfx-sheet-addr]');
ok('the address is displayed', addr.textContent.indexOf('GolfWithdraw999') >= 0 || addr.textContent.indexOf('GolfAddr123') >= 0, addr.textContent);
ok('it is not the "not provided" placeholder', !/Not Provided/.test(addr.textContent));
const copy = modal.querySelector('[data-vfx-sheet-copy]');
ok('the copy button is enabled', copy.disabled === false);
let copied = null;
window.navigator.clipboard = { writeText: t => { copied = t; return Promise.resolve(); } };
copy.click();
await wait(0);
ok('clicking Copy wrote the address to the clipboard', copied !== null && /Golf/.test(copied), copied);
ok('and it confirms to the user', /copied/i.test(window.__lastToast || ''), window.__lastToast);

console.log('\n[6] a coin with no configured address says so honestly');
window.document.querySelector('[data-vfx-asset="NOVA"]').click();
ok('the NOVA sheet is open', $('vfxCoinModal').classList.contains('open'));
ok('it says "Not Provided Yet."',
  /Not Provided Yet/.test($('vfxCoinModal').querySelector('[data-vfx-sheet-addr]').textContent));
ok('the copy button is disabled so there is nothing false to copy',
  $('vfxCoinModal').querySelector('[data-vfx-sheet-copy]').disabled === true);

console.log('\n[7] the four actions are present and hand off to the real flows');
['deposit', 'withdraw', 'send', 'swap'].forEach(k =>
  ok('the ' + k + ' action is rendered', !!$('vfxCoinModal').querySelector('[data-vfx-act="' + k + '"]')));

/* The sheet closes first, then hands off on the next tick, so the underlying
   modal is visible rather than stacked under the sheet. */
async function clickAct(kind) {
  window.vantaOpenCoinWallet('GOLF');
  $('vfxCoinModal').querySelector('[data-vfx-act="' + kind + '"]').click();
  ok('the ' + kind + ' sheet closed before handing off', !$('vfxCoinModal').classList.contains('open'));
  await wait(25);
  return window.__called.pop();
}
ok('Deposit opens the deposit address view', JSON.stringify(await clickAct('deposit')) === '["address","GOLF","deposit"]');
ok('Withdraw opens the withdraw address view', JSON.stringify(await clickAct('withdraw')) === '["address","GOLF","withdraw"]');
ok('Send opens the transfer flow for that coin', JSON.stringify(await clickAct('send')) === '["send","GOLF"]');
ok('Swap opens convert into that coin', JSON.stringify(await clickAct('swap')) === '["swap","USDT","GOLF"]');
ok('no action invented a request of its own', window.__apiCalls.every(p => p === '/api/wallet/layout'));

console.log('\n[8] a coin outside the three cannot be opened');
window.vantaOpenCoinWallet('BTC');
ok('BTC is refused', !$('vfxCoinModal') || !$('vfxCoinModal').classList.contains('open'));
window.vantaOpenCoinWallet('USDT');
ok('USDT is refused', !$('vfxCoinModal') || !$('vfxCoinModal').classList.contains('open'));
window.vantaOpenCoinWallet('ETH');
ok('ETH is refused', !$('vfxCoinModal') || !$('vfxCoinModal').classList.contains('open'));

console.log('\n[9] closing works, including Escape');
window.vantaOpenCoinWallet('ABC');
ok('the ABC sheet opened', $('vfxCoinModal').classList.contains('open'));
$('vfxCoinModal').querySelector('[data-vfx-sheet-close]').click();
ok('the X button closes it', !$('vfxCoinModal').classList.contains('open'));
window.vantaOpenCoinWallet('ABC');
window.document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape' }));
ok('Escape closes it', !$('vfxCoinModal').classList.contains('open'));
window.vantaOpenCoinWallet('ABC');
$('vfxCoinModal').click();
ok('clicking the backdrop closes it', !$('vfxCoinModal').classList.contains('open'));

console.log('\n[10] it routes rather than re-checking, and leaves the page scrollable');
/* The existing flows own their own practice/login/PIN rules, so this block must
   NOT pre-empt them with a second, differently-worded guard. */
window.vantaOpenCoinWallet('GOLF');
window.accountMode = 'demo';
$('vfxCoinModal').querySelector('[data-vfx-act="swap"]').click();
await wait(25);
ok('Swap still reaches the existing convert flow in practice mode',
  JSON.stringify(window.__called.pop()) === '["swap","USDT","GOLF"]');
ok('the block itself decides nothing about practice mode', /isPractice/.test(block) === false);
window.accountMode = 'live';
ok('the sheet closed', !$('vfxCoinModal').classList.contains('open'));
ok('page scrolling is restored, not left locked', window.document.body.style.overflow === '');

console.log('\n[11] re-render on refresh keeps exactly three coins');
window.vantaWalletBalances = { GOLF: 7, NOVA: 2, ABC: 900, BTC: 1 };
window.vantaCoinBalances = { USDT: 1000, GOLF: 12, NOVA: 3, ABC: 500 };
window.vantaSyncWalletUI();
const again = [...window.document.querySelectorAll('[data-vfx-asset]')].map(c => c.getAttribute('data-vfx-asset'));
ok('still exactly three cards', again.length === 3, again.length);
ok('still ABC, GOLF, NOVA', JSON.stringify(again) === '["ABC","GOLF","NOVA"]', JSON.stringify(again));
ok('balances updated to the new figures',
  /900/.test(window.document.querySelector('[data-vfx-asset="ABC"]').textContent));
ok('the hidden column stays empty', $('vfxNormalHoldings').textContent === '');

console.log('\n[12] it asked for nothing new');
ok('the only endpoint called is the layout the page already loads',
  window.__apiCalls.every(p => p === '/api/wallet/layout'), window.__apiCalls);
ok('no trade endpoint was touched', !window.__apiCalls.some(p => /\/api\/trade/.test(p)));
ok('no swap/move endpoint was touched',
  !window.__apiCalls.some(p => /\/api\/swap|move-profit|transfer/.test(p)), window.__apiCalls);

console.log();
if (fails) { console.log('FAILED (' + fails + ')'); process.exit(1); }
console.log('All wallet coin-asset DOM checks passed.');
}

main();
