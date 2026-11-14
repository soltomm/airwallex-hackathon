// Isolated simulated server. Harbour House is the hotel; Groupy is the software (logo and title only).
const {chromium}=require('playwright'),assert=require('node:assert/strict');
const base=process.env.BRAND_TEST_URL||'http://127.0.0.1:8032';
if(!['127.0.0.1','localhost'].includes(new URL(base).hostname))throw Error('Use a local test server.');
(async()=>{
 const b=await chromium.launch();
 try{
  const ctx=await b.newContext({viewport:{width:1440,height:1000}}),p=await ctx.newPage(),errors=[];
  p.on('pageerror',e=>errors.push(e.message));
  await p.route('**/api/state',async route=>{
   const response=await route.fetch(),s=await response.json();
   for(const d of s.deals)d.thread.push({id:'escape-check',at:s.clock,from:'seller',text:"Group Sales, Harbour House <img src=x onerror='window.brandXss=1'>"});
   await route.fulfill({response,json:s});
  });
  await p.goto(base);await p.waitForFunction(()=>window.Harbour?.state());assert.equal(await p.evaluate(()=>window.Harbour.state().provider),'simulated');await p.evaluate(()=>fetch('/api/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}));await p.reload();await p.waitForFunction(()=>window.Harbour?.state()?.deals.some(d=>d.company==='Summit Robotics'&&d.status==='offer_sent'));
  assert.equal(await p.title(),'Groupy · Harbour House group sales');
  assert.match(await p.locator('.wb-logo').innerText(),/Groupy/);
  assert.match(await p.locator('.wb-property').innerText(),/Harbour House/);
  assert.equal(await p.locator('.wb-avatar').innerText(),'HH');
  const d=await p.evaluate(()=>window.Harbour.state().deals.find(d=>d.company==='Summit Robotics'));
  await p.evaluate(id=>window.Harbour.open(id),d.id);await p.locator('[data-tab=conv]').click();
  const conv=await p.locator('#deal').innerText();
  assert.match(conv,/Group Sales, Harbour House/);assert.doesNotMatch(conv,/Group Sales, Groupy/);
  assert.equal(await p.locator('#deal .msg img').count(),0);assert.equal(await p.evaluate(()=>window.brandXss),undefined);
  const buyer=await ctx.newPage();buyer.on('pageerror',e=>errors.push(e.message));
  await buyer.goto(base+d.inbox_url);await buyer.locator('#thread .mail').last().waitFor();
  const inbox=await buyer.locator('body').innerText();
  assert.match(inbox,/Harbour House/);assert.doesNotMatch(inbox,/Groupy/);
  assert.deepEqual(errors,[]);console.log('PASS: Groupy logo/title; Harbour House property, avatar, seller sign-off and buyer mailbox; HTML escaping.');
 }finally{await b.close();}
})().catch(e=>{console.error(e);process.exit(1)});
