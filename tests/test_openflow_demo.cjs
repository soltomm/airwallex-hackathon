const test = require('node:test');
const assert = require('node:assert/strict');
const M = require('../web/openflow-demo.js');
const create = (scenario='available', channel='email') => M.create(channel, scenario);

test('available inquiry -> exact manager approval -> 30% deposit -> handoff', () => {
  const d=create();
  assert.equal(d.status,'draft');
  assert.deepEqual(M.pricing(d),{roomNights:36,total:906000,deposit:271800});
  assert.throws(()=>M.confirm(d,[d]),/approval/);
  M.requestApproval(d,'Revenue manager',[d]);
  M.decide(d,'approve','',[d]); M.confirm(d,[d]);
  assert.equal(d.status,'confirmed'); assert.equal(d.paid,271800);
  assert.match(d.timeline.at(-1).text,/handoff/);
  assert.throws(()=>M.confirm(d,[d]),/approval/);
});

test('phone inquiry uses a wedding transcript and no meeting room',()=>{
  const d=create('available','phone');assert.match(d.original,/Sophie/);assert.equal(d.rooms,8);assert.equal(d.meeting,false);
});

test('unavailable stay -> viable alternative -> reviewed reply -> acceptance',()=>{
  const d=create('unavailable');
  assert.equal(d.status,'unavailable');assert.equal(M.availability(d)[0].free,2);
  assert.throws(()=>M.requestApproval(d,'General manager',[d]));
  assert.throws(()=>M.selectAlternative(d,'2027-04-10',[d]),/full stay/);
  M.selectAlternative(d,'2027-04-11',[d]);
  assert.equal(d.departure,'2027-04-14');assert.match(d.reply,/2027-04-14/);
  assert.throws(()=>M.acceptAlternative(d,[d]),/Send/);
  M.sendAlternative(d,d.reply+' Thank you.');M.acceptAlternative(d,[d]);
  assert.equal(d.status,'draft');assert.equal(d.approval,null);
});

test('checkout night is excluded; every occupied night is checked',()=>{
  assert.deepEqual(M.nights('2027-04-10','2027-04-13'),['2027-04-10','2027-04-11','2027-04-12']);
  const d=create('unavailable');
  assert.equal(M.fits(d,[],'2027-04-09','2027-04-12'),false);
  assert.equal(M.fits(d,[],'2027-04-11','2027-04-14'),true);
  assert.equal(M.fits(d,[],'2027-04-29','2027-04-30'),true);
  assert.equal(M.fits(d,[],'2027-04-29','2027-05-01'),false);
});

test('rate, date and service edits invalidate approved terms',()=>{
  for(const patch of [{rate:23000},{arrival:'2027-04-11',departure:'2027-04-14'},{breakfast:false},{meeting:false}]){
    const d=create();M.requestApproval(d,'General manager',[d]);M.decide(d,'approve','',[d]);
    M.revise(d,patch);assert.equal(d.approval,null);assert.equal(d.status,'draft');
    assert.throws(()=>M.confirm(d,[d]),/approval/);
  }
});

test('stale manager reviews cannot approve revised pending terms',()=>{
  const d=create();M.requestApproval(d,'General manager',[d]);d.rate+=100;
  assert.throws(()=>M.decide(d,'approve'),/stale/);
});

test('decline requires a reason and returns the proposal for revision',()=>{
  const d=create();M.requestApproval(d,'General manager',[d]);
  assert.throws(()=>M.decide(d,'decline'),/reason/);
  M.decide(d,'decline','Reduce the meeting costs.');
  assert.equal(d.status,'draft');assert.equal(d.revisionReason,'Reduce the meeting costs.');
});

test('confirmed groups consume inventory and cannot be edited',()=>{
  const all=[];
  for(let i=0;i<2;i++){
    const d=create();all.push(d);M.requestApproval(d,'Revenue manager',all);M.decide(d,'approve','',all);M.confirm(d,all);
    assert.throws(()=>M.revise(d,{rate:24000}),/confirmed/);
  }
  const d=create();all.push(d);
  assert.equal(M.fits(d,all),false);
  assert.throws(()=>M.requestApproval(d,'Revenue manager',all),/Inventory/);
});

test('inventory loss after approval blocks confirmation',()=>{
  const all=[create(),create(),create()];
  all.forEach(d=>M.requestApproval(d,'Revenue manager',all));
  all.forEach(d=>M.decide(d,'approve','',all));
  M.confirm(all[0],all);M.confirm(all[1],all);
  assert.throws(()=>M.confirm(all[2],all),/Inventory/);
});

test('invalid rates, stays, channels, and approvers are refused',()=>{
  const d=create();assert.throws(()=>M.revise(d,{rate:NaN}));assert.throws(()=>M.revise(d,{rate:9999}));
  assert.throws(()=>M.revise(d,{departure:d.arrival}));assert.throws(()=>M.nights('bad','2027-04-13'));
  assert.throws(()=>M.create('other'));assert.throws(()=>M.requestApproval(d,'Intern',[d]));
});
