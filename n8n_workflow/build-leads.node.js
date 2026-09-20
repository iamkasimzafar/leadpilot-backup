// Assembles the results the backend expects: one company per Google Maps
// lead, with its contacts. Reads the earlier nodes by name, each inside a
// try/catch, because on a run with no websites (or no WhatsApp check) those
// nodes never executed.
const hook = $('Webhook1').first().json;
const body = hook.body || hook;
const whatsappOn = body.validate_whatsapp === true;

const leads = $('Filter & Keep Phone Leads').all().map(i => i.json).filter(l => !l._no_leads);

const last10 = phone => {
  const digits = String(phone || '').replace(/[^0-9]/g, '');
  return digits.length >= 10 ? digits.slice(-10) : digits;
};
const domainOf = email => (String(email || '').includes('@') ? String(email).split('@')[1].toLowerCase() : '');
const nameKey = (first, last) => `${String(first || '').trim().toLowerCase()}_${String(last || '').trim().toLowerCase()}`;

// --- 1. WhatsApp status per phone -------------------------------------------
// Read from THIS node's input rather than from the validator node by name:
// when the check ran, the validator's output is exactly what arrives here, so
// renaming or recreating that node can never break the lookup again. When the
// check was skipped, the input is the phone list, which carries no status.
//
// The validator answers one row per number: { phone_number, status: "valid" |
// "invalid" }. "valid" = the number is on WhatsApp. A row with no recognisable
// status (an API error, a timeout) leaves that number unchecked (null), so it
// is neither shown as reachable nor billed as a check.
const POSITIVE = ['valid', 'exists', 'active', 'true', 'yes', 'registered'];
const NEGATIVE = ['invalid', 'not active', 'inactive', 'not found', 'false', 'no', 'not registered'];

const whatsapp = {};
if (whatsappOn) {
  const rows = $input.all();
  let phones = [];
  try { phones = $('Unique Phones').all(); } catch (e) {}

  rows.forEach((res, index) => {
    let row = res.json || {};
    // Some responses wrap the rows in an array property.
    const nested = Object.values(row).find(v => Array.isArray(v) && v.length && typeof v[0] === 'object');
    if (nested) row = nested[0];

    let verdict = null;
    if (row.exists === true || row.has_whatsapp === true) verdict = 'Active';
    else if (row.exists === false || row.has_whatsapp === false) verdict = 'Not Active';
    else {
      const status = String(row.status ?? row.whatsapp_status ?? '').trim().toLowerCase();
      if (POSITIVE.includes(status)) verdict = 'Active';
      else if (NEGATIVE.includes(status)) verdict = 'Not Active';
    }
    if (verdict === null) return; // not a result row, or an error: leave unchecked

    // The row names its own number; the position is only a fallback, and only
    // safe when every number produced exactly one row.
    const phone = row.phone_number || row.phone || row.number
      || (rows.length === phones.length && phones[index] ? phones[index].json.clean_phone : '');
    const key = last10(phone);
    if (key) whatsapp[key] = verdict;
  });
}
const whatsappFor = phone => {
  if (!whatsappOn || !phone) return null;
  const verdict = whatsapp[last10(phone)];
  return verdict === undefined ? null : verdict;
};

// --- 2. Decision makers found through the website (Snov.io) -------------------
const people = {};          // nameKey -> { position, source_page, domain }
try {
  for (const item of $('Limit Prospects').all()) {
    for (const p of Array.isArray(item.json.data) ? item.json.data : []) {
      people[nameKey(p.first_name, p.last_name)] = {
        position: p.position || p.title || null,
        source_page: p.source_page || null,
        domain: p.lp_domain || '',
      };
    }
  }
} catch (e) {}

const byEmail = {};         // email -> prospect record from the email search
try {
  for (const item of $('Get prospect email search result').all()) {
    const d = item.json.data;
    if (!d || Array.isArray(d) || !Array.isArray(d.emails)) continue;
    for (const e of d.emails) if (e && e.email) byEmail[e.email.toLowerCase()] = d;
  }
} catch (e) {}

const makersByDomain = {};
const seenEmails = new Set();
const addMaker = (record, email, status) => {
  if (!email || seenEmails.has(email.toLowerCase())) return;
  const known = people[nameKey(record.first_name, record.last_name)] || {};
  const domain = known.domain || domainOf(email);
  if (!domain) return;
  seenEmails.add(email.toLowerCase());
  (makersByDomain[domain] = makersByDomain[domain] || []).push({
    full_name: `${record.first_name || ''} ${record.last_name || ''}`.trim() || 'Decision Maker',
    job_title: record.position || known.position || null,
    verified_email: email,
    email_status: status,
    linkedin_url: record.linkedin_url || record.source_page || known.source_page || null,
    phone_number: null,
    whatsapp_status: null,
  });
};

try {
  for (const item of $('Collect Emails').all()) {
    const d = item.json.data;
    if (Array.isArray(d)) {
      // Email-verifier result: [{ email, result: { smtp_status } }]
      const row = d[0] || {};
      const status = (row.result && row.result.smtp_status) || row.status;
      if (row.email && status === 'valid') addMaker(byEmail[row.email.toLowerCase()] || {}, row.email, 'valid');
    } else if (d && Array.isArray(d.emails) && d.emails[0]) {
      if (d.emails[0].smtp_status === 'valid') addMaker(d, d.emails[0].email, 'valid');
    }
  }
} catch (e) {}

// --- 3. One company per lead ----------------------------------------------------
const companies = [];
for (const lead of leads) {
  const status = whatsappFor(lead.clean_phone);
  const phone = '+' + lead.clean_phone;

  // The business's own line is the primary contact: it carries the phone,
  // the WhatsApp result, and the first email found on its website.
  const contacts = [{
    full_name: lead.name,
    job_title: 'Main business line',
    verified_email: lead.emails[0] || null,
    email_status: lead.emails[0] ? 'unverified' : null,
    linkedin_url: lead.socials.linkedin || null,
    phone_number: phone,
    whatsapp_status: status,
  }];

  for (const maker of (lead.domain && makersByDomain[lead.domain]) || []) contacts.push(maker);

  companies.push({
    company_name: lead.name,
    website: lead.website,
    location: lead.full_address,
    industry: lead.type,
    company_size: null,
    hq_phone: phone,
    decision_makers: contacts,

    // No column of their own; the backend keeps them with the company.
    source: 'google_maps',
    has_website: Boolean(lead.website),
    rating: lead.rating,
    review_count: lead.review_count,
    place_link: lead.place_link,
    google_verified: lead.verified,
    categories: lead.subtypes,
    city: lead.city,
    state: lead.state,
    country: lead.country,
    latitude: lead.latitude,
    longitude: lead.longitude,
    emails: lead.emails,
    socials: lead.socials,
    whatsapp_status: status,
  });
}

return companies.map(company => ({ json: company }));
