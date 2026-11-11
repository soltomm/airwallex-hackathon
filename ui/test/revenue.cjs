// An isolated, simulated server only: resets its scenario and completes revenue review + checkout.
const {chromium}=require('playwright'),assert=require('node:assert/strict');
const base=process.env.REVENUE_TEST_URL||'http://127.0.0.1:8026';
if(!['localhost','127.0.0.1'].includes(new URL(base).hostname))throw Error('Use an isolated loopback server.');
(async()=>{
 const b=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',args:['--no-sandbox']});
 try{
  const ctx=await b.newContext({viewport:{width:1440,height:1000}}),p=await ctx.newPage(),errors=[];
  p.on('pageerror',e=>errors.push(e.message));await p.goto(base);
  const state=()=>p.evaluate(()=>fetch('/api/state').then(r=>r.json()));
  assert.equal((await state()).provider,'simulated');
  await p.evaluate(()=>fetch('/api/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}));
  await p.reload();
  await p.waitForFunction(()=>window.Harbour.state()?.deals.some(d=>d.company==='Summit Robotics'&&d.status==='offer_sent'));
  let d=(await state()).deals.find(d=>d.company==='Summit Robotics'),id=d.id;
  await p.evaluate(async d=>{const r=await fetch('/api/buyer/'+d.id,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:d.buyer_token,action:'counter',text:'Can you do £179 per room/night?',payload:{rate:17900}})});if(!r.ok)throw Error(await r.text());},d);
  await p.waitForFunction(id=>window.Harbour.state()?.deals.find(d=>d.id===id)?.pricing_review?.status==='pending',id);
  assert.match(await p.locator('#wbTable').innerText(),/Awaiting revenue review/);
  await p.evaluate(id=>window.Harbour.open(id),id);
  await p.getByRole('button',{name:'Review as revenue manager',exact:true}).click();
  assert.match(await p.locator('#revenueReview').innerText(),/£179.00/);
  await p.locator('#revenueNote').fill('Internal only: retain contribution margin.');
  await p.getByRole('button',{name:'Save internal note',exact:true}).click();
  await p.waitForFunction(id=>window.Harbour.state()?.deals.find(d=>d.id===id)?.pricing_review?.notes.some(n=>n.text==='Internal only: retain contribution margin.'),id);
  assert.match(await p.locator('#revenuePanel').innerText(),/Internal only: retain contribution margin/);
  await p.locator('#revenueRate').fill('100');
  await p.getByRole('button',{name:'Preview selected price',exact:true}).click();
  await p.getByText(/below the absolute minimum/).waitFor();
  assert.equal(await p.getByRole('button',{name:'Approve and requote this package',exact:true}).isDisabled(),true);
  await p.locator('#revenueRate').fill('179');
  await p.getByRole('button',{name:'Preview selected price',exact:true}).click();
  await p.getByRole('button',{name:'Approve and requote this package',exact:true}).waitFor();
  assert.equal(await p.getByRole('button',{name:'Approve and requote this package',exact:true}).isDisabled(),false);
  assert.match(await p.locator('#revenuePanel').innerText(),/40% deposit/);
  // The same protected workflow link opens the existing inquiry drawer for the demo reviewer.
  const review=await ctx.newPage();await review.goto(base+'/#pricing-review/'+id);
  await review.locator('#revenuePanel').waitFor();assert.equal(await review.locator('#revenuePanel').isVisible(),true);
  await review.close();
  await p.setViewportSize({width:390,height:844});
  assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await p.setViewportSize({width:1440,height:1000});
  await p.screenshot({path:'/tmp/revenue-review.png',fullPage:false});
  await p.getByRole('button',{name:'Approve and requote this package',exact:true}).click();
  await p.waitForFunction(id=>window.Harbour.state()?.deals.find(d=>d.id===id)?.status==='offer_sent',id);
  d=(await state()).deals.find(d=>d.id===id);
  assert.equal(d.versions.at(-1).terms.rate,17900);assert.equal(d.pricing_review.status,'approved');
  const buyer=await ctx.newPage();await buyer.goto(base+d.inbox_url);
  const text=await buyer.locator('body').innerText();assert(!/Internal only|Agent’s rate limit|contribution margin/.test(text));
  await buyer.evaluate(async d=>{const r=await fetch('/api/buyer/'+d.id,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:d.buyer_token,action:'accept',payload:{version:d.versions.at(-1).n}})});if(!r.ok)throw Error(await r.text());},d);
  await p.waitForFunction(id=>window.Harbour.state()?.deals.find(d=>d.id===id)?.status==='awaiting_payment',id);
  d=(await state()).deals.find(d=>d.id===id);
  const pay=await ctx.newPage();await pay.goto(d.pay_url);await pay.getByRole('link',{name:/Pay deposit/}).click();await pay.getByRole('button',{name:/Pay/}).click();
  await p.waitForFunction(id=>window.Harbour.state()?.deals.find(d=>d.id===id)?.booking?.status==='confirmed',id);
  assert.deepEqual(errors,[]);console.log('PASS: automatic revenue routing, smart summary, internal notes/link, hard-limit refusal, mobile, approved requote, buyer privacy and existing checkout.');
 }finally{await b.close();}
})().catch(e=>{console.error(e);process.exit(1)});
