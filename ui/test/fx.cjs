// Isolated local server only: FX_TEST_URL defaults to port 8014. See docs/FX_POLICY.md.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const base=process.env.FX_TEST_URL||'http://127.0.0.1:8014';
if(!['localhost','127.0.0.1'].includes(new URL(base).hostname))throw Error('Run this money-flow simulation only against an isolated local server.');
const shots=path.resolve(__dirname,'../../docs/screenshots/fx');
(async()=>{
 fs.mkdirSync(shots,{recursive:true});
 const browser=await chromium.launch({...(process.env.CHROMIUM_PATH||fs.existsSync('/usr/bin/chromium')?{executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium'}:{}),headless:true,args:['--no-sandbox']});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1080}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(base);
  const provider=await page.evaluate(()=>fetch('/api/state').then(r=>r.json()).then(s=>s.provider));
  assert.equal(provider,'simulated');
  await page.evaluate(()=>fetch('/api/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}));
  await page.reload();await page.getByRole('button',{name:'Northstar Analytics'}).first().waitFor();
  const state=()=>page.evaluate(()=>fetch('/api/state').then(r=>r.json()));
  // Unknown country stays unknown until it is explicitly recorded.
  assert.match(await page.locator('#wbTable').innerText(),/Payer origin unknown/);
  await page.locator('#wbRules').click();await page.locator('#fxDirectoryOpen').click();
  await page.locator('#fxSearch').waitFor();
  assert.equal(await page.locator('#fxCountryRows tr').count(),250);
  await page.locator('#fxSearch').fill('United States');
  assert.match(await page.locator('#fxCountryRows').innerText(),/USD[\s\S]*Stable currency/);
  await page.locator('#fxSearch').fill('Nigeria');
  assert.match(await page.locator('#fxCountryRows').innerText(),/NGN[\s\S]*Unstable currency/);
  await page.screenshot({path:path.join(shots,'directory.png')});
  await page.locator('#fxSearch').fill('Panama');
  assert.match(await page.locator('#fxCountryRows').innerText(),/PAB[\s\S]*USD/);
  await page.locator('#fxSearch').fill('Antarctica');
  assert.match(await page.locator('#fxCountryRows').innerText(),/No recorded national tender/);
  await page.locator('#fxSearch').fill('');await page.locator('#fxFilter').selectOption('unstable');
  assert((await page.locator('#fxCountryRows tr').count())>0);
  assert((await page.locator('#fxCountryRows tr').count())<250);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#fxDirectoryOpen').evaluate(el=>el===document.activeElement),true);
  await page.getByRole('button',{name:'Overview',exact:true}).click();
  // Stable foreign currency: record a provider-verified simulated receipt and hold it.
  await page.getByRole('button',{name:'Northstar Analytics'}).first().click();
  await page.locator('#fxPayerCountry').selectOption('US');
  await page.locator('#fxSavePayer').click();
  await page.getByText('Foreign bank receipt monitoring',{exact:true}).click();
  await page.locator('#fxSimulate').click();
  await page.waitForFunction(()=>document.querySelector('.fx-outcome')?.textContent.includes('held'));
  await page.keyboard.press('Escape');
  assert.match(await page.locator('#wbTable').innerText(),/United States[\s\S]*USD[\s\S]*held USD/);
  // Heightened-risk currency: automatic conversion and no booking confirmation from treasury funds.
  await page.getByRole('button',{name:'Summit Robotics'}).first().click();
  await page.locator('#fxPayerCountry').selectOption('NG');await page.locator('#fxSavePayer').click();
  await page.getByText('Foreign bank receipt monitoring',{exact:true}).click();await page.locator('#fxSimulate').click();
  await page.waitForFunction(()=>document.querySelector('.fx-outcome')?.textContent.includes('converted'));
  let s=await state();const summit=s.deals.find(d=>d.company==='Summit Robotics');
  assert.equal(summit.payer_fx.latest.currency,'NGN');assert.equal(summit.payer_fx.latest.status,'converted');
  assert.equal(summit.booking,null);
  const id=summit.payer_fx.latest.conversion_id;
  await page.getByText('Foreign bank receipt monitoring',{exact:true}).click();await page.locator('#fxScan').click();
  s=await state();assert.equal(s.deals.find(d=>d.company==='Summit Robotics').payer_fx.latest.conversion_id,id);
  await page.screenshot({path:path.join(shots,'conversion.png')});
  await page.keyboard.press('Escape');
  await page.locator('#wbSearch').fill('Nigeria');await page.waitForTimeout(1700);
  assert.equal(await page.locator('#wbTable tbody tr').count(),1);
  await page.locator('#wbSearch').fill('');await page.locator('#wbTitle').click();
  await page.screenshot({path:path.join(shots,'pipeline.png')});
  // Browser reload retains origin, conversion identity and status.
  await page.reload();await page.getByRole('button',{name:'Summit Robotics'}).first().waitFor();
  assert.match(await page.locator('#wbTable').innerText(),/Nigeria[\s\S]*converted NGN/);
  await page.setViewportSize({width:390,height:844});
  await page.waitForFunction(()=>!document.querySelector('.wb-bottom').open);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.getByText('Property tools',{exact:true}).click();await page.locator('#wbRules').click();
  await page.locator('#fxDirectoryOpen').click();await page.locator('#fxSearch').fill('Nigeria');
  assert.equal(await page.locator('#fxDirectory').evaluate(el=>el.scrollWidth<=el.clientWidth),true);
  await page.screenshot({path:path.join(shots,'mobile-directory.png')});
  assert.deepEqual(errors,[]);
  console.log('PASS: country directory, risk tags, multicurrency/no-tender cases, focus, explicit payer origin, USD hold, NGN conversion, duplicate protection, booking separation, search, persistence, mobile.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
