/* First-run guide + account PIN.
 *
 * The contract being protected: a brand-new account is walked through the
 * basics, then made to create the ONE account PIN, and that PIN is the same one
 * every other operation already asks for. Nothing here may add a second PIN
 * store or a second verification path.
 *
 * Usage: node test_first_run_pin.js <path to index.html>
 */
const fs = require('fs');
const path = require('path');

const file = process.argv[2] || path.join(__dirname, 'index.html');
const src = fs.readFileSync(file, 'utf8');

let fails = 0;
const ok = (label, cond, detail) => {
  if (cond) console.log('  ok   ' + label);
  else { fails++; console.log('  FAIL ' + label + (detail !== undefined ? ' -- ' + JSON.stringify(detail) : '')); }
};

console.log('\n[1] the guide runs on first entry, for both login and signup');
ok('it is called from the one funnel both paths share', /vantaOnboardMaybeShow\(\)/.test(src));
ok('and it is awaited last, after the app is drawn',
  /setInterval\(refreshGolfStats, 20000\);[\s\S]{0,700}vantaOnboardMaybeShow\(\)/.test(src));
ok('a failure in the guide cannot block entering the app',
  /try \{ await window\.vantaOnboardMaybeShow\(\); \} catch \(e\) \{ \/\* a guide must never block entry \*\/ \}/.test(src));
ok('it needs a session, because the PIN is attached to the account',
  /if\(typeof vantaToken!=='function' \|\| !vantaToken\(\)\) return;/.test(src));

console.log('\n[2] "new" means the account has no PIN yet, per the server');
ok('the decision comes from the shared PIN status, not a local flag',
  /window\.vantaPinStatus\(\)\s*\.then\(has=>\{ if\(!has\) showGuide\(\); \}\)/.test(src));
ok('no localStorage flag was invented for this', !/vanta_onboard|vanta_onboarded|vanta_intro_seen/.test(src));
ok('and it follows the existing pre-existing-flag style rather than a new key',
  !/localStorage\.setItem\([^)]*(?:onboard|intro|guide)/i.test(src));
ok('a returning account is never asked again', /if\(finished\) return;/.test(src));
ok('a failed status check is swallowed, not thrown at the user', /\.catch\(\(\)=>\{ \/\* never block entering the app on a guide \*\/ \}\)/.test(src));

console.log('\n[3] it is really one PIN, the one every operation already uses');
ok('the guide opens the shared gate, not a PIN dialog of its own',
  /window\.vantaRequirePin\(/.test(src));
ok('no second PIN dialog markup was added', (src.match(/id="vantaPinModal"/g) || []).length === 1);
ok('still only one PIN store key in the file', (src.match(/vanta_demo_pin_v1/g) || []).length <= 2,
  'a second PIN store was introduced');
ok('the required PIN is verified by the existing server round-trip',
  /'\/api\/wallet\/pin\/verify'/.test(src) && /vantaApi\('\/api\/wallet\/pin'\)/.test(src));
ok('the guide does not hash or store digits itself',
  !/vob[A-Za-z]*\s*=\s*[^;]*pin/i.test(src) && !/localStorage[^;]*vob/i.test(src));

console.log('\n[4] it cannot be skipped, but it does not trap anyone either');
ok('the PIN step is mandatory: no Cancel on that prompt', /mandatory:true/.test(src));
ok('and the shared dialog honours it', /cancel\.style\.display=o\.mandatory\?'none':'block';/.test(src));
ok('backing out lands back on the PIN step, not past it',
  /onCancel:\(\)=>\{ next\.disabled=false; showPin\(\); \}/.test(src));
ok('the guide has no backdrop-click dismiss', !/vantaOnboard[\s\S]{0,400}e\.target===modal/.test(src));
ok('Escape cannot close it either',
  /e\.key==='Escape' && modal\.classList\.contains\('open'\)\)\{ e\.preventDefault\(\); if\(onPinStep\) showPin\(\); \}/.test(src));
ok('a finished guide closes and stops re-opening', /function finish\(\)\{ finished=true; modal\.classList\.remove\('open'\); \}/.test(src));

console.log('\n[5] the guide sits under the PIN prompt, and the rest is presentation');
ok('the guide is below the PIN dialog so the prompt lands on top',
  /#vantaOnboard\{position:fixed;inset:0;z-index:100550/.test(src) && /#vantaPinModal\{position:fixed;inset:0;z-index:100600/.test(src));
ok('it reuses the existing stepper classes', /class="vpm-step on" id="vobStep1"/.test(src) && /id="vobStep2"/.test(src));
ok('it is wired open/close with the house .open convention',
  /modal\.classList\.add\('open'\)/.test(src) && /modal\.classList\.remove\('open'\)/.test(src));
ok('it has both steps in the markup', /id="vobList"/.test(src) && /id="vobNote"/.test(src));
ok('a Next button is the only way forward on step one', /id="vobNext"[^>]*>Next</.test(src));
ok('light and dark themes are both covered',
  /html\[data-theme="dark"\] \.vob-card\{/.test(src) && /html\[data-theme="dark"\] \.vob-list li\{/.test(src));
ok('no new endpoint and no new network call were introduced',
  !/vantaApi\('\/api\/(?!wallet\/pin)[a-z-]+/.test(src.split('vanta-first-run-js')[0].split('vanta-safe-features-js')[1] || ''));
ok('the redundant signup welcome toast is gone, so there is one first-run message',
  !/title: "Welcome to VANTA TRADE"/.test(src));

console.log('\n[6] the PIN wording says what it is for');
ok('the note says the PIN is required', /you need a <b>4–5 digit PIN<\/b> for this account/.test(src));
ok('and that it covers every operation', /required by every operation: deposit, withdraw, send, swap/.test(src));
ok('and that it is stored on the account, not the browser',
  /stored as a hash on your account — never in this browser/.test(src));

console.log();
if (fails) { console.log('FAILED (' + fails + ')'); process.exit(1); }
console.log('All first-run guide and PIN checks passed.');
