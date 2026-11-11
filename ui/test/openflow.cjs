// Run against an isolated simulated Harbour server; see docs/OPENFLOW_UX.md.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {execFileSync}=require('node:child_process');
const base=process.env.OPENFLOW_TEST_URL||'http://127.0.0.1:8011';
const screenshots=path.resolve(__dirname,'../../docs/screenshots/openflow');
(async()=>{
 fs.mkdirSync(screenshots,{recursive:true});
 const browser=await chromium.launch({...(process.env.CHROMIUM_PATH||fs.existsSync('/usr/bin/chromium')?{executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium'}:{}),headless:true,args:['--no-sandbox']});
 try {
 const context=await browser.newContext({viewport:{width:1440,height:1100}});
 const page=await context.newPage(), errors=[], posts=[];
 const shot=(name,fullPage=true)=>page.screenshot({path:path.join(screenshots,name),fullPage,style:'.toast, .wb-feedback { visibility:hidden; }'});
 page.on('pageerror',e=>errors.push(e.message));
 page.on('request',r=>{if(r.method()==='POST')posts.push(new URL(r.url()).pathname);});
 // Start from the seeded scenario: a second run would otherwise find seed 4242 already sent ("replay refused") and
 // wait forever for its deal to open. Resetting is only safe on an isolated, simulated, loopback server.
 if(!['localhost','127.0.0.1'].includes(new URL(base).hostname))throw Error('Use an isolated loopback server.');
 assert.equal((await (await context.request.get(base+'/api/state')).json()).provider,'simulated');
 assert.ok((await context.request.post(base+'/api/reset',{data:{}})).ok());
 const before=execFileSync('git',['show','origin/main:web/group_sales.html'],{cwd:path.resolve(__dirname,'../..'),encoding:'utf8'});
 await page.route(base+'/',r=>r.fulfill({status:200,contentType:'text/html',body:before}));
 await page.goto(base);
 if(await page.locator('#workbench').count()) await page.locator('#wbTable .wb-inquiry').first().waitFor();
 else await page.getByText('Northstar Analytics',{exact:true}).first().waitFor();
 await shot('before.png');
 await page.unroute(base+'/'); await page.reload();
 await page.getByText('Groupy · Connected',{exact:true}).first().waitFor();
 const startCount=await page.locator('.wb-table tbody tr').count();
 // Live Harbour detail and support tools retain their working controls.
 await page.getByRole('button',{name:'Northstar Analytics'}).first().click();
 await page.locator('#liveDrawer[open]').waitFor();
 await page.locator('#deal .tabs').waitFor();
 await page.getByRole('button',{name:'Payments',exact:true}).last().click();
 assert.match(await page.locator('#deal').innerText(),/No payments yet|Ledger|received|Airwallex/i);
 await page.keyboard.press('Escape');
 await page.locator('#wbRules').click();await page.getByText('Seller rules',{exact:true}).last().waitFor();
 await page.getByRole('button',{name:'Overview',exact:true}).click();
 // New inquiry opens by keyboard, lists every preset from the server and offers the buyer-mode switch.
 await page.locator('#wbSimulate').focus();await page.keyboard.press('Enter');
 await page.waitForFunction(()=>document.activeElement?.id==='wbDrawerTitle',null,{timeout:2000});
 await page.locator('[data-preset="full_lifecycle"]').waitFor();
 for(const p of ['straight_booking','dates_short_haggle','full_lifecycle','payer_abroad','missing_details'])
  assert.equal(await page.locator(`[data-preset="${p}"]`).count(),1,p);
 assert.equal(await page.locator('[data-buyer-mode="presenter"]').getAttribute('aria-pressed'),'true');
 await shot('simulator.png',false);
 // A bad seed is refused in the drawer; a good one sends the preset and opens the real deal.
 await page.locator('#wbSeed').fill('abc');await page.locator('[data-preset="full_lifecycle"]').click();
 assert.match(await page.locator('#wbDrawer .wb-feedback').innerText(),/whole number/);
 await page.locator('#wbSeed').fill('4242');await page.locator('[data-preset="full_lifecycle"]').click();
 await page.locator('#liveDrawer[open]').waitFor();
 const gen=await page.evaluate(()=>window.Harbour.state().deals.find(d=>d.generated?.seed===4242));
 assert.equal(gen.generated.preset,'full_lifecycle');
 assert.match(await page.locator('#deal').innerText(),new RegExp(gen.company));
 await page.keyboard.press('Escape');
 await page.getByText('Generated · Full lifecycle with a refund · seed 4242',{exact:true}).waitFor();
 // Replaying the seed in the same run is refused, not merged into the first deal.
 await page.locator('#wbSimulate').click();await page.locator('#wbSeed').fill('4242');
 await page.locator('[data-preset="full_lifecycle"]').click();
 assert.match(await page.locator('#wbDrawer .wb-feedback').innerText(),/already has an inquiry/);
 // Traits chosen by hand: a payer abroad with details left out.
 await page.locator('#wbSeed').fill('77');
 await page.getByText('Or choose the traits yourself').click();
 await page.locator('#wbT_payer_country').selectOption('DE');
 await page.locator('[name=wbMissing][value=taxes]').check();
 await page.locator('#wbCustom').click();
 await page.locator('#liveDrawer[open]').waitFor();await page.keyboard.press('Escape');
 await page.getByText('Generated · Custom traits · seed 77',{exact:true}).waitFor();
 // The buyer-mode switch changes the server's setting.
 await page.locator('#wbSimulate').click();await page.locator('[data-buyer-mode="agent"]').click();
 await page.waitForFunction(()=>window.Harbour.state().runner.settings.buyer_mode==='agent');
 assert.equal(await page.locator('[data-buyer-mode="agent"]').getAttribute('aria-pressed'),'true');
 await page.locator('[data-buyer-mode="presenter"]').click();
 await page.waitForFunction(()=>window.Harbour.state().runner.settings.buyer_mode==='presenter');
 // Write your own: a real RFP with no brief.
 await page.locator('#wbInquiryFrom').fill('Jamie Chen <jamie@acme.example>');
 await page.locator('#wbInquiryText').fill('Please quote 6 king rooms for 12 colleagues arriving 10 May and departing 13 May 2027, with breakfast. Our budget is £4,500 inclusive of taxes. Acme Demo Ltd pays.');
 await page.locator('#wbSend').click();
 await page.locator('#liveDrawer[open]').waitFor();await page.keyboard.press('Escape');
 // Search keeps focus through a polling tick; filters work.
 await page.locator('#wbSearch').fill('Acme');await page.waitForTimeout(1700);
 assert.equal(await page.locator('#wbSearch').evaluate(el=>el===document.activeElement),true);
 assert.equal(await page.locator('.wb-table tbody tr').count(),1);
 await page.locator('#wbSearch').fill('');
 await page.getByRole('button',{name:'All inquiries',exact:true}).click();
 assert.equal(await page.locator('.wb-table tbody tr').count(),startCount+3);
 await page.getByRole('button',{name:'Payments',exact:true}).first().click();
 const pay=await page.locator('#wbBody').innerText();
 assert.match(pay,/Groupy simulated provider|Airwallex sandbox/);
 assert.doesNotMatch(pay,/browser fixture/);
 await page.getByRole('button',{name:'Overview',exact:true}).click();
 await shot('overview.png');
 await page.setViewportSize({width:390,height:844});
 await page.waitForFunction(()=>!document.querySelector('.wb-bottom').open);
 await page.getByText('Property tools',{exact:true}).click();
 await page.locator('#wbAvailability').click();
 await page.getByRole('heading',{name:'Availability',exact:true}).waitFor();
 await page.getByRole('button',{name:'Overview',exact:true}).click();
 await page.getByText('Property tools',{exact:true}).click();
 await shot('mobile.png');
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 await page.locator('#wbSimulate').click();await page.locator('[data-preset="full_lifecycle"]').waitFor();
 await page.getByText('Or choose the traits yourself').click();
 await shot('mobile-simulator.png',false);
 assert.equal(await page.locator('#wbDrawer').evaluate(el=>el.scrollWidth<=el.clientWidth),true);
 await page.keyboard.press('Escape');
 assert.equal(await page.locator('#wbSimulate').evaluate(el=>el===document.activeElement),true);
 // The workbench only writes through the intended endpoints.
 assert.deepEqual([...new Set(posts)].sort(),['/api/inbox','/api/settings','/api/simulate/rfp']);
 assert.deepEqual(errors,[],'No browser runtime errors');
 console.log('PASS: connected Harbour detail/rules, presets from the server, seeded and custom generated inquiries, replay refused, buyer-mode switch, write your own, search/polling, filters, payments, keyboard focus, mobile.');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
