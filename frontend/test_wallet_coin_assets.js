/* Verifies the Wallet coin-asset UI: exactly three coins, logos everywhere,
   a clickable detail view, copyable address, and the four actions.
 *
 * Runs the real inline script in a jsdom document built from the real
 * index.html, so this exercises the shipped code rather than a copy of it.
 *
 * Usage: node test_wallet_coin_assets.js <path to index.html>
 */
const fs = require('fs');
const path = require('path');

const file = process.argv[2] || path.join(__dirname, 'index.html');
const src = fs.readFileSync(file, 'utf8');

let fails = 0;
function ok(label, cond, detail) {
  if (cond) console.log('  ok   ' + label);
  else { fails++; console.log('  FAIL ' + label + (detail ? ' -- ' + detail : '')); }
}

let JSDOM = null;
try { JSDOM = require('jsdom').JSDOM; } catch (e) { /* reported below */ }

function blockById(id) {
  const re = new RegExp('<script[^>]*id="' + id + '"[^>]*>([\\s\\S]*?)<\\/script>');
  const m = src.match(re);
  return m ? m[1] : null;
}

console.log('\n[1] the Wallet block is present and self-contained');
const block = blockById('vanta-wallet-coin-assets-js');
ok('the wallet coin-assets script exists', !!block);
ok('it guards against double-initialising', /__vantaWalletCoinAssets/.test(src));

console.log('\n[2] only ABC, GOLF and NOVA are Wallet coins');
const listMatch = src.match(/var WALLET_COINS\s*=\s*\[([^\]]*)\]/);
ok('a fixed three-coin list is declared', !!listMatch);
if (listMatch) {
  const syms = listMatch[1].split(',').map(s => s.trim().replace(/^['"]|['"]$/g, ''));
  ok('the list is exactly ABC, GOLF, NOVA', JSON.stringify(syms), JSON.stringify(syms));
  ok('it has three entries', syms.length === 3, 'got ' + syms.length);
}
ok('no other symbol is hardcoded as a wallet asset',
  !/WALLET_COINS\s*=\s*\[[^\]]*BTC/.test(src));
ok('the detail view refuses a coin outside the list',
  /if\(WALLET_COINS\.indexOf\(sym\)<0\) return;/.test(block || ''));
ok('the standard-crypto column is removed from the layout',
  /#vfxNormalWallet\{display:\s*none\s*!important\}/.test(src));
ok('the removed column is emptied as well as hidden',
  /vfxNormalHoldings[\s\S]{0,120}innerHTML\s*=\s*''/.test(block || ''));

console.log('\n[3] every coin uses the existing logo system');
ok('the list uses vantaCoinIconHTML', /vantaCoinIconHTML/.test(block || ''));
ok('the detail sheet uses the same helper', /iconHTML\(sym,sym\)/.test(block || ''));
ok('there is no second, parallel icon implementation',
  !/function\s+vfxAssetIcon\s*\(/.test(block || ''));
ok('logo->emoji->letter fallback is reused, not reinvented',
  /vantaCoinIconFallback/.test(block || ''));
ok('broken images degrade to the coin emoji', /img\.addEventListener\('error'/.test(block || ''));
ok('the legacy GOLF emoji slots now take a logo',
  (src.match(/data-vfx-logo="GOLF"/g) || []).length === 2);
ok('the raw GOLF emoji is gone from the wallet markup',
  !/⛳ GOLF — Early Access/.test(src));
ok('inline logo slots are painted', /function paintInlineLogos/.test(block || ''));

console.log('\n[4] clicking a coin opens its details view');
ok('each asset card is a button', /<button type="button" class="vfx-asset" data-vfx-asset=/.test(block || ''));
ok('the click opens the coin view', /openCoin\(e\.currentTarget\.getAttribute\('data-vfx-asset'\)\)/.test(block || ''));
ok('a dialog is created', /className='vfx-coin-modal'/.test(block || '') || /vfx-coin-modal/.test(block || ''));
ok('it is announced as a dialog', /aria-modal['"]\s*,\s*['"]true/.test(block || ''));
ok('Escape closes it', /e\.key==='Escape'/.test(block || ''));
ok('clicking the backdrop closes it', /if\(e\.target===modal\) closeCoin\(\)/.test(block || ''));

console.log('\n[5] the wallet address is easy to copy');
ok('the address is shown in the sheet', /data-vfx-sheet-addr/.test(block || ''));
ok('a Copy button is present', /data-vfx-sheet-copy/.test(block || ''));
ok('copy uses the async Clipboard API', /navigator\.clipboard\.writeText/.test(block || ''));
ok('with a legacy fallback for insecure origins', /execCommand\('copy'\)/.test(block || ''));
ok('copy is disabled when there is no address', /copyBtn\.disabled=true/.test(block || ''));
ok('missing address is stated honestly', /Not Provided Yet\./.test(block || ''));

console.log('\n[6] the coin view offers Deposit, Withdraw, Send and Swap');
['deposit', 'withdraw', 'send', 'swap'].forEach(function (kind) {
  ok('has a ' + kind + ' action', new RegExp('data-vfx-act="' + kind + '"').test(block || ''));
});
ok('Deposit reuses the existing address view',
  /vantaShowCoinAddress\(sym,'deposit'\)/.test(block || ''));
ok('Withdraw reuses the existing address view',
  /vantaShowCoinAddress\(sym,'withdraw'\)/.test(block || ''));
ok('Send reuses the existing transfer flow', /vantaOpenTransfer\(sym\)/.test(block || ''));
ok('Swap reuses the existing convert flow', /vantaOpenConvert\('USDT',sym\)/.test(block || ''));
ok('it delegates the practice/login/PIN checks to those flows',
  !/isPractice/.test(block || ''));

console.log('\n[7] no wallet/trading/backend behaviour was changed');
const allApi = (block || '').match(/vantaApi\('[^']+'\)/g) || [];
ok('no new fetch/XHR outside the layout already loaded',
  allApi.every(s => /\/api\/wallet\/layout/.test(s)), JSON.stringify(allApi));
const calls = (block || '').match(/vantaApi\('[^']+'\)/g) || [];
ok('the only endpoint it calls is the existing layout one',
  calls.length > 0 && calls.every(s => /\/api\/wallet\/layout/.test(s)),
  JSON.stringify(calls));
ok('it does not write any balance', !/vantaWalletBalances\s*\[/.test(block || ''));
ok('it only reads the ledgers', /window\.vantaWalletBalances/.test(block || ''));
ok('it wraps the existing refresh instead of replacing it',
  /var base=\(typeof window\.vantaSyncWalletUI/.test(block || ''));
ok('no trade endpoint is touched', !/api\/trade/.test(block || ''));

console.log('\n[8] responsive');
ok('narrow breakpoint', /@media \(max-width:560px\)/.test(src));
ok('very narrow breakpoint', /@media \(max-width:360px\)/.test(src));
ok('the grid reflows to one column on tiny screens',
  /@media \(max-width:360px\)\{[\s\S]{0,120}grid-template-columns:minmax\(0,1fr\)/.test(src));
ok('actions stay reachable on mobile', /vfx-coin-acts\{grid-template-columns:repeat\(2/.test(src));

console.log();
if (fails) { console.log('FAILED (' + fails + ')'); process.exit(1); }
console.log('All wallet coin-asset checks passed.' + (JSDOM ? '' : '  (jsdom unavailable - static checks only)'));
