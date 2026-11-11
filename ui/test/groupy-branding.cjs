// Isolated simulated server. Checks new branding and legacy display without rewriting records.
const {chromium}=require('playwright'),assert=require('node:assert/strict');
const base=process.env.BRAND_TEST_URL||'http://127.0.0.1:8032';
if(!['127.0.0.1','localhost'].includes(new URL(base).hostname))throw Error('Use a local test server.');
(async()=>{
 const b=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 try{
  const ctx=await b.newContext({viewport:{width:1440,height:1000}}),p=await ctx.newPage(),errors=[];
  p.on('pageerror',e=>errors.push(e.message));
  await p.route('**/api/state',async route=>{
   const response=await route.fetch(),s=await response.json();
   for(const d of s.deals)d.thread.push({id:'legacy-brand',at:s.clock,from:'seller',text:"Legacy Group Sales, Harbour House <img src=x onerror='window.brandXss=1'>"});
   await route.fulfill({response,json:s});
  });
  await p.goto(base);await p.waitForFunction(()=>window.Harbour?.state());assert.equal(await p.evaluate(()=>window.Harbour.state().provider),'simulated');await p.evaluate(()=>fetch('/api/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}));await p.reload();await p.waitForFunction(()=>window.Harbour?.state()?.deals.some(d=>d.company==='Summit Robotics'&&d.status==='offer_sent'));
  const clean=async page=>assert(!/harbour\s+house|harbourhouse\.example/i.test(await page.locator('body').innerText()));
  await clean(p);assert.equal(await p.title(),'Groupy · Group sales');
  assert.equal(await p.locator('.wb-avatar').innerText(),'G');
  for(const hash of ['#directory','#rules','#availability','']){await p.evaluate(hash=>location.hash=hash,hash);await p.waitForTimeout(150);await clean(p);}
  let d=await p.evaluate(()=>window.Harbour.state().deals.find(d=>d.company==='Summit Robotics'));
  await p.evaluate(id=>window.Harbour.open(id),d.id);await p.locator('[data-tab=conv]').click();
  assert.match(await p.locator('#deal .msg').first().innerText(),/Legacy Group Sales, Groupy/);
  assert.equal(await p.locator('#deal .msg img').count(),0);assert.equal(await p.evaluate(()=>window.brandXss),undefined);await clean(p);
  await p.screenshot({path:'/tmp/groupy-branding.png'});
  const buyer=await ctx.newPage();buyer.on('pageerror',e=>errors.push(e.message));
  await buyer.route('**/api/buyer/**',async route=>{
   if(route.request().method()!=='GET'){await route.continue();return;}
   const response=await route.fetch(),v=await response.json();
   if(v.thread)v.thread.push({id:'legacy-brand',at:v.clock,from:'seller',text:'Legacy Group Sales, Harbour House'});
   await route.fulfill({response,json:v});
  });
  await buyer.goto(base+d.inbox_url);await buyer.locator('#thread .mail').last().waitFor();
  assert.match(await buyer.locator('#thread').innerText(),/Groupy/);await clean(buyer);
  // Use the actual stored quote and normal buyer acceptance, then check the hosted payment UI.
  await buyer.evaluate(async d=>{const r=await fetch('/api/buyer/'+d.id,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:d.buyer_token,action:'accept',payload:{version:d.versions.at(-1).n}})});if(!r.ok)throw Error(await r.text());},d);
  await p.waitForFunction(id=>window.Harbour.state().deals.find(d=>d.id===id)?.status==='awaiting_payment',d.id);
  d=await p.evaluate(id=>window.Harbour.state().deals.find(d=>d.id===id),d.id);
  const pay=await ctx.newPage();await pay.goto(d.pay_url);await clean(pay);assert.match(await pay.locator('#title').innerText(),/Groupy/);
  await p.locator('#liveDrawer [data-wb-close]').click();await p.locator('#wbSimulate').click();await p.locator('#wbSeed').waitFor();await clean(p);
  assert.deepEqual(errors,[]);console.log('PASS: dashboard/property/conversation/new-inquiry/Directory/rules/availability/mailbox/payment branding; legacy text display and HTML escaping.');
 }finally{await b.close();}
})().catch(e=>{console.error(e);process.exit(1)});
