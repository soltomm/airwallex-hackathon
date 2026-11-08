/* Isolated browser-only fixtures. No Harbour API, credentials, or payment provider calls. */
(function (root, factory) {
  const model = factory();
  if (typeof module === 'object' && module.exports) module.exports = model;
  else root.OpenFlowDemo = model;
})(typeof window === 'undefined' ? globalThis : window, function () {
  'use strict';
  const plus = (date, days) => new Date(Date.parse(date + 'T12:00:00Z') + days * 864e5).toISOString().slice(0, 10);
  function nights(arrival, departure) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(arrival) || !/^\d{4}-\d{2}-\d{2}$/.test(departure)) throw Error('Choose valid stay dates.');
    if (!Number.isFinite(Date.parse(arrival)) || !Number.isFinite(Date.parse(departure)) || plus(arrival, 0) !== arrival || plus(departure, 0) !== departure) throw Error('Choose valid calendar dates.');
    const length = (Date.parse(departure) - Date.parse(arrival)) / 864e5;
    if (!Number.isInteger(length) || length < 1 || length > 14) throw Error('Choose a stay of 1–14 nights.');
    return Array.from({length}, (_, i) => plus(arrival, i));
  }
  const inventory = Object.fromEntries(Array.from({length: 20}, (_, i) => [plus('2027-04-10', i), 28]));
  const stamp = (d, text) => d.timeline.push({at: new Date().toISOString(), text});
  const signature = d => JSON.stringify([d.arrival, d.departure, d.rooms, d.rate, d.breakfast, d.meeting]);
  const pricing = d => {
    const roomNights = nights(d.arrival, d.departure).length * d.rooms;
    const total = roomNights * (d.rate + (d.breakfast ? 1500 : 0)) + (d.meeting ? 60000 : 0);
    return {roomNights, total, deposit: Math.round(total * .3)};
  };
  function availability(d, all = [], arrival = d.arrival, departure = d.departure) {
    return nights(arrival, departure).map(date => {
      const booked = all.filter(x => x.id !== d.id && x.status === 'confirmed')
        .reduce((n, x) => n + (nights(x.arrival, x.departure).includes(date) ? x.rooms : 0), 0);
      const capacity = inventory[date] ?? 0;
      const scenarioCapacity = d.scenario === 'unavailable' && date === '2027-04-10' ? 2 : capacity;
      const free = Math.max(0, scenarioCapacity - booked);
      return {date, free, required: d.rooms, fits: free >= d.rooms};
    });
  }
  const fits = (d, all, arrival, departure) => availability(d, all, arrival, departure).every(n => n.fits);
  function create(channel, scenario = 'random', all = []) {
    if (!['email', 'phone'].includes(channel)) throw Error('Choose an email or phone inquiry.');
    if (scenario === 'random') scenario = Math.random() < .5 ? 'available' : 'unavailable';
    if (!['available', 'unavailable'].includes(scenario)) throw Error('Choose an inventory scenario.');
    const phone = channel === 'phone';
    const d = {id: 'demo-' + (typeof crypto !== 'undefined' && crypto.randomUUID ? crypto.randomUUID() : Date.now() + '-' + Math.random()),
      demo: true, channel, scenario, company: phone ? 'The Bennett wedding' : 'Acme leadership offsite',
      contact: phone ? 'Sophie Bennett' : 'Jamie Chen · People & Culture', arrival: '2027-04-10', departure: '2027-04-13',
      rooms: phone ? 8 : 12, rate: phone ? 19000 : 22000, breakfast: true, meeting: !phone,
      status: 'draft', manager: 'Revenue manager', approval: null, paid: 0, reply: '', timeline: [],
      original: phone ? 'Sample call transcript\nSophie: We are getting married nearby and need eight rooms for 16 guests, arriving 10 April and departing 13 April 2027. Could you include breakfast? No meeting space is needed.\nHotel: We will check the full stay and get back to you with a proposal.' : 'From: Jamie Chen <jamie@acme.example>\nSubject: Leadership offsite · 10–13 April 2027\nHello, we would love 12 rooms for our leadership offsite, arriving 10 April and departing 13 April 2027. Please include breakfast and a meeting room. Could you send us a group proposal? Thank you, Jamie.'};
    stamp(d, phone ? 'Simulated phone inquiry received; sample transcript extracted.' : 'Simulated email RFP received; sample requirements extracted.');
    d.status = fits(d, all) ? 'draft' : 'unavailable';
    stamp(d, d.status === 'draft' ? 'Every occupied night checked. Proposal ready to prepare.' : 'Inventory shortage found. Alternative dates required.');
    return d;
  }
  function revise(d, patch) {
    if (d.status === 'confirmed') throw Error('A confirmed booking cannot be edited in this demo.');
    const next = {...d};
    for (const k of ['arrival', 'departure', 'rate', 'breakfast', 'meeting']) if (k in patch) next[k] = patch[k];
    nights(next.arrival, next.departure);
    if (!Number.isSafeInteger(next.rate) || next.rate < 10000 || next.rate > 100000) throw Error('Nightly rate must be £100–£1,000.');
    const changed = signature(next) !== signature(d);
    Object.assign(d, next);
    if (changed) {
      d.approval = null;
      d.reply = '';
      d.status = 'draft';
      stamp(d, 'Terms revised. Any previous approval and alternative reply were invalidated.');
    }
    return d;
  }
  function selectAlternative(d, arrival, all) {
    if (d.status === 'confirmed') throw Error('This booking is already confirmed.');
    const departure = plus(arrival, nights(d.arrival, d.departure).length);
    if (!fits(d, all, arrival, departure)) throw Error('The full stay must be available, including every occupied night.');
    revise(d, {arrival, departure});
    d.status = 'alternative_draft';
    d.reply = `Hello ${d.contact.split(' · ')[0]}, thank you for your inquiry. We can offer ${d.rooms} rooms from ${arrival} to ${departure} (checkout), with ${d.breakfast ? 'breakfast included' : 'room-only accommodation'}. Would these alternative dates work for your group?`;
    stamp(d, 'Viable alternative selected. Draft reply ready for hotel review.');
  }
  function sendAlternative(d, reply) {
    if (d.status !== 'alternative_draft' || !reply.trim()) throw Error('Review an alternative-date reply before sending.');
    d.reply = reply.trim(); d.status = 'alternative_sent';
    stamp(d, 'Hotel reviewed and sent the alternative reply (simulated).');
  }
  function acceptAlternative(d, all) {
    if (d.status !== 'alternative_sent') throw Error('Send the reviewed reply first.');
    if (!fits(d, all)) throw Error('Availability has changed. Select another stay.');
    d.status = 'draft'; stamp(d, 'Customer accepted the alternative (simulated). Preparing a fresh proposal.');
  }
  function requestApproval(d, manager, all) {
    if (!['Revenue manager', 'General manager'].includes(manager)) throw Error('Choose a manager.');
    if (!['draft', 'proposal'].includes(d.status)) throw Error('Prepare the proposal before requesting approval.');
    if (!fits(d, all)) throw Error('Inventory is unavailable for this stay.');
    d.manager = manager; d.revisionReason = '';
    d.approval = {signature: signature(d), status: 'pending', manager};
    d.status = 'approval'; stamp(d, `Approval link delivered to ${manager} (simulated).`);
  }
  function decide(d, decision, reason = '', all = []) {
    if (d.status !== 'approval' || d.approval?.status !== 'pending' || d.approval.signature !== signature(d)) throw Error('This approval is missing or stale. Request a new approval.');
    if (!['approve', 'decline'].includes(decision)) throw Error('Choose approve or decline.');
    if (decision === 'approve' && !fits(d, all)) throw Error('Inventory is no longer available.');
    if (decision === 'decline' && !reason.trim()) throw Error('Include a reason for the requested revision.');
    d.approval.status = decision === 'approve' ? 'approved' : 'declined';
    d.status = decision === 'approve' ? 'proposal' : 'draft';
    d.revisionReason = decision === 'decline' ? reason.trim() : '';
    stamp(d, decision === 'approve' ? `${d.manager} approved the exact proposed terms (simulated).` : `Revision requested: ${reason.trim()}`);
  }
  function confirm(d, all) {
    if (d.status !== 'proposal' || d.approval?.status !== 'approved' || d.approval.signature !== signature(d)) throw Error('Current terms need manager approval before confirmation.');
    if (!fits(d, all)) throw Error('Inventory is no longer available across the full stay.');
    d.paid = pricing(d).deposit; d.status = 'confirmed';
    stamp(d, 'Customer accepted. A 30% deposit was recorded (simulated; no money moved).');
    stamp(d, 'Booking confirmed. Operations handoff is ready.');
  }
  return {plus, nights, inventory, pricing, availability, fits, create, revise, selectAlternative, sendAlternative, acceptAlternative, requestApproval, decide, confirm};
});
