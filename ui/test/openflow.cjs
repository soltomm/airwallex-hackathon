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
 // Start from the seeded scenario, so the inquiry counts below are exact. Resetting is only safe on an isolated, simulated, loopback server.
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
 await page.getByText('Harbour House · Connected',{exact:true}).first().waitFor();
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
 // New inquiry opens by keyboard with "Write your own" first; generated test inquiries are folded below it,
 // list every preset the server offers, and there is no hand-picked traits form.
 await page.locator('#wbSimulate').focus();await page.keyboard.press('Enter');
 await page.waitForFunction(()=>document.activeElement?.id==='wbDrawerTitle',null,{timeout:2000});
 await page.locator('#wbInbound').waitFor();
 assert.equal(await page.locator('#wbInbound').isVisible(),true);
 assert.equal(await page.locator('[data-preset="full_lifecycle"]').isVisible(),false,'generated inquiries start folded');
 const order=await page.evaluate(()=>{const f=document.querySelector('#wbInbound'),g=document.querySelector('.wb-generated');return !!(f.compareDocumentPosition(g)&Node.DOCUMENT_POSITION_FOLLOWING);});
 assert.equal(order,true,'write your own comes before generated inquiries');
 assert.equal(await page.locator('#wbCustom, [id^="wbT_"]').count(),0,'no traits form');
 await page.getByText('Generated test inquiries',{exact:true}).click();
 await page.locator('[data-preset="full_lifecycle"]').waitFor();
 for(const p of ['straight_booking','dates_short_haggle','full_lifecycle','missing_details'])
  assert.equal(await page.locator(`[data-preset="${p}"]`).count(),1,p);
 assert.equal(await page.locator('[data-preset="payer_abroad"]').count(),0,'payer_abroad needs GS_FX=1');
 assert.equal(await page.locator('[data-buyer-mode], .wb-buyer-mode').count(),0,'no buyer agent switch');
 await shot('simulator.png',false);
 // No seed field: each click sends a new inquiry and opens its real deal.
 assert.equal(await page.locator('#wbSeed, .wb-seed').count(),0,'no seed field');
 const generated=()=>page.evaluate(()=>window.Harbour.state().deals.filter(d=>d.generated));
 await page.locator('[data-preset="full_lifecycle"]').click();
 await page.locator('#liveDrawer[open]').waitFor();
 const [gen]=await generated();
 assert.equal(gen.generated.preset,'full_lifecycle');
 assert.match(await page.locator('#deal').innerText(),new RegExp(gen.company));
 await page.keyboard.press('Escape');
 await page.getByText('Generated · Full lifecycle with a refund',{exact:true}).waitFor();
 // The same preset again is a different inquiry, not a replay.
 await page.locator('#wbSimulate').click();await page.getByText('Generated test inquiries',{exact:true}).click();
 await page.locator('[data-preset="full_lifecycle"]').click();
 await page.locator('#liveDrawer[open]').waitFor();
 const again=await generated();
 assert.equal(again.length,2);assert.notEqual(again[0].id,again[1].id);assert.notEqual(again[0].generated.seed,again[1].generated.seed);
 await page.keyboard.press('Escape');await page.locator('#liveDrawer:not([open])').waitFor({state:'attached'});
 await page.locator('#wbSimulate').click();
 // Write your own: a real RFP with no brief.
 await page.locator('#wbInquiryFrom').fill('Jamie Chen <jamie@acme.example>');
 await page.locator('#wbInquiryText').fill('Please quote 6 king rooms for 12 colleagues arriving 10 May and departing 13 May 2027, with breakfast. Our budget is £4,500 inclusive of taxes. Acme Demo Ltd pays.');
 await page.locator('#wbSend').click();
 await page.locator('#liveDrawer[open]').waitFor();await page.keyboard.press('Escape');
 // Closing hands focus back to the page in a later task; let it land before typing, as a person would.
 await page.locator('#liveDrawer:not([open])').waitFor({state:'attached'});await page.waitForTimeout(300);
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
 await page.locator('#wbSimulate').click();await page.locator('#wbInbound').waitFor();
 await page.getByText('Generated test inquiries',{exact:true}).click();await page.locator('[data-preset="full_lifecycle"]').waitFor();
 await shot('mobile-simulator.png',false);
 assert.equal(await page.locator('#wbDrawer').evaluate(el=>el.scrollWidth<=el.clientWidth),true);
 await page.keyboard.press('Escape');
 assert.equal(await page.locator('#wbSimulate').evaluate(el=>el===document.activeElement),true);
 // The workbench only writes through the intended endpoints.
 assert.deepEqual([...new Set(posts)].sort(),['/api/inbox','/api/simulate/rfp']);
 assert.deepEqual(errors,[],'No browser runtime errors');
 console.log('PASS: connected Harbour detail/rules, presets from the server, write your own first, folded presets, no traits form, no seed field, a new inquiry each click, no buyer agent switch, write your own, search/polling, filters, payments, keyboard focus, mobile.');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
