// Browser check of the OpenFlow workbench. Run it against an isolated simulated Harbour server
// (it adds two real inquiries); see docs/OPENFLOW_UX.md:
//
//   PAYMENTS=simulated SELLER_MODE=rules GS_VAR=var/openflow PORT=8011 PYTHONPATH=src python scripts/group_sales.py &
//   node ui/test/openflow.cjs            # OPENFLOW_TEST_URL defaults to http://127.0.0.1:8011
//
// It saves screenshots to docs/screenshots/openflow/; restore them (git checkout docs/screenshots)
// unless you mean to update them.
//
// What it covers:
// - connected Harbour inquiries open Harbour's own controls in the live drawer; seller rules and
//   availability open from the property tools;
// - "+ New inquiry" sends real inquiries to Harbour House (POST /api/inbox), from both samples;
// - inquiries saved by the earlier browser-only simulator (localStorage openflow-harbour-demo-v1) still
//   display and work: exact-terms approval, revision, deposit, handoff, alternative dates. New ones are
//   no longer created, so the test seeds them the way an old browser would have them;
// - search survives polling, filters, payments, persistence, keyboard focus, phone width.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const base=(process.env.OPENFLOW_TEST_URL||'http://127.0.0.1:8011').replace(/\/$/,'');
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
 const rowCount=()=>page.locator('.wb-table tbody tr[data-open]').count();
 // Harbour files a repeat of the same email (sender + text) as a duplicate, so each run tags its inquiries.
 const run=Date.now().toString(36);
 const tag=async()=>{const t=page.locator('#wbInquiryText');await t.fill(`${await t.inputValue()} Test run ${run}.`);};
 await page.goto(base);
 await page.getByText('Harbour House · Connected',{exact:true}).first().waitFor({timeout:60000});
 const startCount=await rowCount();

 // Live Harbour detail and support tools retain their working controls.
 await page.getByRole('button',{name:'Northstar Analytics'}).first().click();
 await page.locator('#liveDrawer[open]').waitFor();
 await page.locator('#deal .tabs').waitFor();
 await page.getByRole('button',{name:'Payments',exact:true}).last().click();
 assert.match(await page.locator('#deal').innerText(),/No payments yet|Ledger|received|Airwallex/i);
 await page.keyboard.press('Escape');
 await page.locator('#wbRules').click();await page.getByText('Seller rules',{exact:true}).last().waitFor();
 await page.getByRole('button',{name:'Overview',exact:true}).click();

 // New inquiry, opened by keyboard: the email sample (rooms that are free) goes to Harbour House.
 await page.locator('#wbSimulate').focus();await page.keyboard.press('Enter');
 assert.equal(await page.locator('#wbDrawerTitle').evaluate(el=>el===document.activeElement),true);
 await page.locator('#wbScenario').selectOption('available');
 await page.getByRole('button',{name:/Sample email RFP/}).click();
 assert.match(await page.locator('#wbInquiryFrom').inputValue(),/jamie@acme\.example/);
 assert.match(await page.locator('#wbInquiryText').inputValue(),/6 king rooms for 12 colleagues/);
 await tag(); await page.locator('#wbSend').click();
 await page.locator('#liveDrawer[open] #deal .title').waitFor();
 assert.match(await page.locator('#deal .title').innerText(),/Acme/i);
 assert.equal(await page.locator('#wbDrawer').evaluate(el=>el.open),false);
 await page.keyboard.press('Escape');
 // The phone sample asking for more rooms than are free: also a real inquiry; the agent offers what it has.
 await page.locator('#wbSimulate').click();await page.locator('#wbScenario').selectOption('unavailable');
 await page.getByRole('button',{name:/Sample phone inquiry/}).click();
 assert.match(await page.locator('#wbInquiryText').inputValue(),/20 king rooms for 40 guests/);
 await shot('simulator.png',false);
 await tag(); await page.locator('#wbSend').click();
 await page.locator('#liveDrawer[open] #deal .title').waitFor();
 assert.match(await page.locator('#deal .title').innerText(),/Bennett/i);
 await page.keyboard.press('Escape');
 await page.waitForFunction(n=>document.querySelectorAll('.wb-table tbody tr[data-open]').length===n,startCount+2);
 assert.deepEqual(posts,['/api/inbox','/api/inbox'],'New inquiry posts each inquiry to Harbour House once');

 // Inquiries an earlier browser-only simulator saved still display, labelled, and still work.
 await page.evaluate(()=>{
  const M=window.OpenFlowDemo, saved=[M.create('email','available'),M.create('phone','unavailable')];
  localStorage.setItem('openflow-harbour-demo-v1',JSON.stringify(saved));
 });
 await page.reload();
 await page.locator('#wbTable').getByRole('button',{name:'Acme leadership offsite'}).waitFor();
 assert.match(await page.locator('#wbTable').innerText(),/Email · Simulated/);
 assert.equal(await rowCount(),startCount+4);
 await page.locator('#wbTable').getByRole('button',{name:'Acme leadership offsite'}).click();
 assert.match(await page.locator('#wbDrawerBody').innerText(),/every occupied night has sufficient inventory/);
 await shot('proposal.png',false);
 await page.locator('#wbManager').selectOption('General manager');
 await page.getByRole('button',{name:'Send link · simulated'}).click();
 const reviewLink=await page.locator('.wb-link').getAttribute('href');
 await page.getByRole('button',{name:'Open manager review'}).click();
 await page.getByRole('button',{name:'Decline / request revision'}).click();
 assert.match(await page.locator('#wbDrawer .wb-feedback').innerText(),/reason/);
 await page.getByRole('button',{name:'Approve exact terms'}).click();
 await page.getByRole('button',{name:'Simulate acceptance + 30% deposit'}).waitFor();
 // Unsaved edits cannot slip through approved terms.
 await page.locator('[name=rate]').fill('240');
 await page.getByRole('button',{name:'Simulate acceptance + 30% deposit'}).click();
 assert.equal(await page.getByRole('button',{name:'Simulate acceptance + 30% deposit'}).count(),0);
 await page.getByRole('button',{name:'Send link · simulated'}).click();
 await page.getByRole('button',{name:'Open manager review'}).click();
 await page.locator('#wbReason').fill('Please use the original room rate.');
 await page.getByRole('button',{name:'Decline / request revision'}).click();
 assert.match(await page.locator('#wbDrawerBody').innerText(),/Please use the original room rate/);
 await page.locator('[name=rate]').fill('220');
 await page.getByRole('button',{name:'Save revised terms'}).click();
 await page.getByRole('button',{name:'Send link · simulated'}).click();
 await page.locator('.wb-link').click();
 await page.getByRole('button',{name:'Approve exact terms'}).click();
 await page.getByRole('button',{name:'Simulate acceptance + 30% deposit'}).click();
 assert.match(await page.locator('#wbDrawerBody').innerText(),/Operations handoff/);
 assert.match(await page.locator('#wbDrawerBody').innerText(),/£2,718/);
 await page.keyboard.press('Escape');
 // The saved unavailable stay: full-stay calendar, alternative dates, reviewed reply.
 await page.locator('#wbTable').getByRole('button',{name:'The Bennett wedding'}).click();
 assert.match(await page.locator('#wbDrawerBody').innerText(),/We don’t have room for the full stay/);
 assert.equal(await page.locator('[data-date="2027-04-10"]').isDisabled(),true);
 await shot('unavailable.png',false);
 await page.getByRole('button',{name:'1 day later',exact:true}).click();
 assert.match(await page.locator('#wbReply').inputValue(),/2027-04-11.*2027-04-14/);
 await page.locator('#wbReply').fill((await page.locator('#wbReply').inputValue())+' We would love to host your wedding party.');
 await page.getByRole('button',{name:'Send reviewed reply · simulated'}).click();
 await page.getByRole('button',{name:'Simulate customer acceptance'}).click();
 assert.match(await page.locator('#wbDrawerBody').innerText(),/Prepare the proposal/);
 await page.keyboard.press('Escape');

 // Search keeps focus through a polling tick; filters and row open work by keyboard. Closing the drawer
 // hands focus back to its opener, so search only once it has closed.
 await page.locator('#wbDrawer').waitFor({state:'hidden'});await page.waitForTimeout(300);
 await page.locator('#wbSearch').fill('Bennett wedding');await page.waitForTimeout(1700);
 assert.equal(await page.locator('#wbSearch').evaluate(el=>el===document.activeElement),true);
 assert.equal(await rowCount(),1);
 await page.locator('#wbSearch').fill('');
 await page.getByRole('button',{name:'Confirmed',exact:true}).click();
 assert.equal(await rowCount(),1);
 await page.getByRole('button',{name:'All inquiries',exact:true}).click();
 assert.equal(await rowCount(),startCount+4);
 await page.getByRole('button',{name:'Payments',exact:true}).first().click();
 assert.match(await page.locator('#wbBody').innerText(),/Simulated · browser fixture/);
 assert.match(await page.locator('#wbBody').innerText(),/£2,718/);
 await page.getByRole('button',{name:'Overview',exact:true}).click();
 await shot('overview.png');
 // Fresh page proves fixture persistence and opens a manager link in this browser.
 await page.reload();await page.getByRole('button',{name:'The Bennett wedding'}).first().waitFor();
 await page.goto(base+'/'+reviewLink);await page.locator('#wbDrawer[open]').waitFor();
 await page.keyboard.press('Escape');

 // Phone width.
 await page.setViewportSize({width:390,height:844});
 await page.waitForFunction(()=>!document.querySelector('.wb-bottom').open);
 await page.getByText('Property tools',{exact:true}).click();
 await page.locator('#wbAvailability').click();
 await page.getByRole('heading',{name:'Availability',exact:true}).waitFor();
 await page.getByRole('button',{name:'Overview',exact:true}).click();
 await page.getByText('Property tools',{exact:true}).click();
 await shot('mobile.png');
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 await page.locator('#wbSimulate').click();
 await shot('mobile-simulator.png',false);
 assert.equal(await page.locator('#wbDrawer').evaluate(el=>el.scrollWidth<=el.clientWidth),true);
 await page.keyboard.press('Escape');
 assert.equal(await page.locator('#wbSimulate').evaluate(el=>el===document.activeElement),true);

 assert.deepEqual(posts,['/api/inbox','/api/inbox'],'Only New inquiry writes to Harbour; saved simulator fixtures never POST');
 assert.deepEqual(errors,[],'No browser runtime errors');
 console.log('PASS: connected Harbour detail/rules, New inquiry to Harbour House (both samples), saved simulator fixtures (exact-terms approval, stale approval, revision, deposit, handoff, alternative dates), search/polling, filters, payments, persistence, keyboard focus, mobile.');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
