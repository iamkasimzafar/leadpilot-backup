# LeadPilot (B2B) workflow: closing the dead ends

Why this matters: in n8n a node that outputs **zero items** simply stops that
branch. No error is raised, so the Error Trigger never fires, nothing is
posted to the backend, and the user's search sits at "running" until the
backend's sweeper fails it 30 minutes later. Every fix below turns a silent
stop into either a continued run or an immediate, named failure.

The named failures are thrown as `LeadPilot Error: <code> - <detail>`. The
Error Trigger workflow forwards the message, the backend recognises the code,
and the app shows a plain sentence for it. Codes the backend knows:
`no_companies_found`, `no_company_size_match`, `no_decision_makers_found`,
`no_valid_emails_found`.

A guard can only run if it receives at least one item, so each filter in front
of a guard needs **Settings → Always Output Data = ON** (it then emits one
empty item instead of nothing).

---

## 1. Backlisting (Code) — replace the last line

```js
// was: return output;
if (!output.length) {
  throw new Error('LeadPilot Error: no_companies_found - the web search returned no usable company websites for these keywords.');
}
return output;
```

## 2. Filter (AI score) — setting + new guard node

1. Open **Filter** → Settings → **Always Output Data: ON**.
2. Add a Code node named **Guard: AI Qualified** between `Filter` and
   `Start domain search` (mode: Run Once for All Items):

```js
const real = $input.all().filter(i => i.json && i.json.domain);
if (!real.length) {
  const seen = $('Parse DeepSeek Response').all().length;
  throw new Error(`LeadPilot Error: no_companies_found - none of the ${seen} search results qualified as a matching company website.`);
}
return real;
```

## 3. Filter the companies (Code) — two bugs, replace the ending

Bug A: the "200+ employees" band sends `company_size_max: null`, and
`actualRange.min <= null` is always false in JavaScript, so **every** company
is dropped for that band. Bug B: an empty result stops the run silently.

Replace the line that computes `isWithinRange`:

```js
// was: const isWithinRange = (actualRange.min <= targetMax) && (actualRange.max >= targetMin);
const aboveMin = targetMin === null || actualRange.max >= targetMin;
const belowMax = targetMax === null || actualRange.min <= targetMax;
const isWithinRange = aboveMin && belowMax;
```

Replace the final `return`:

```js
// was: return keptItems.slice(0, MAX_COMPANIES);
if (!keptItems.length) {
  throw new Error(`LeadPilot Error: no_company_size_match - ${$input.all().length} companies were found but none matched the requested company size.`);
}
return keptItems.slice(0, MAX_COMPANIES);
```

## 4. Verified emails are currently lost — new node after Keep Only Valid

`Get email verification result` returns `{ data: [ { email, result } ] }`
(an array). `If1`, `Code in JavaScript` and `Group Leads` all expect the
prospect shape `{ data: { emails: [...] } }`, so every email that went through
verification is treated as "no email": it is dropped from the results **and**
sent down the failure branch.

Add a Code node named **Restore Prospect** between `Keep Only Valid` and
`Merge` (mode: **Run Once for Each Item**):

```js
// Put the verified status back on the original prospect record.
const prospect = JSON.parse(JSON.stringify($('Switch').item.json));
const verified = ($json.data && $json.data[0]) || {};
if (prospect.data && Array.isArray(prospect.data.emails) && prospect.data.emails[0]) {
  prospect.data.emails[0].smtp_status = (verified.result && verified.result.smtp_status) || 'valid';
}
return { json: prospect };
```

Also on **Keep Only Valid** → Settings → **Always Output Data: ON**.

## 5. Switch `no_email` output — connect it

Drag the unconnected **no_email** output of `Switch` to **Merge (Input 1)**,
the same input the `valid` output uses. With this, `Merge` always receives
every prospect, so it always runs, and the guard in step 6 decides.

## 6. Replace If1 + HTTP Request1 with one guard

`If1` routes item by item, so on a normal run the contacts without an email
go to `HTTP Request1`, which tells the backend the **whole search failed**
even though results are being sent on the other branch.

1. Delete **If1** and **HTTP Request1**.
2. Add a Code node named **Guard: Valid Emails** between `Merge` and
   `Code in JavaScript` (Run Once for All Items):

```js
const withEmail = $input.all().filter(i => {
  const d = i.json && i.json.data;
  const e = d && !Array.isArray(d) && Array.isArray(d.emails) ? d.emails[0] : null;
  return e && e.email && e.smtp_status === 'valid';
});
if (!withEmail.length) {
  throw new Error(`LeadPilot Error: no_valid_emails_found - none of the ${$input.all().length} decision makers had a verifiable email address.`);
}
return withEmail;
```

Keep the `Merge → Verifying Contacts` connection as it is.

## 7. Has Phone Number? — remove the dead end

Its **false** output goes nowhere. When no contact has a phone number the
true branch is empty too, `Group Leads` never runs, and good email leads are
never sent. (`Group Leads` reads its contacts from `Merge` directly, so the
phone check only needs to decide who gets a WhatsApp check.)

1. Delete **Has Phone Number?** and connect `Code in JavaScript` →
   `Remove duplicate`.
2. Replace the **Remove duplicate** code:

```js
// Unique phone numbers for the WhatsApp check. Never returns nothing: with
// no phone at all it emits a marker so the run continues to Group Leads.
const seen = new Set();
const phones = [];
for (const item of $input.all()) {
  const phone = item.json.clean_phone;
  if (!phone || seen.has(phone)) continue;
  seen.add(phone);
  phones.push(item);
}
return phones.length ? phones : [{ json: { _no_phones: true } }];
```

3. Change the **If** condition (the WhatsApp switch) to:

```
{{ $('Webhook').first().json.body?.validate_whatsapp === true && $json._no_phones !== true }}
```

## 8. Group Leads & Apply Abandon Rule — replace the last line

```js
// was: return qualifiedCompanies.map(company => ({ json: { ...company } }));
if (!qualifiedCompanies.length) {
  throw new Error('LeadPilot Error: no_valid_emails_found - no company was left with a verified decision maker.');
}
return qualifiedCompanies.map(company => ({ json: { ...company } }));
```

## 9. Send Results to Backend — stop hard-coding the tunnel

URL is currently a fixed ngrok address. Every ngrok restart breaks it, and the
app then shows "finished, results never arrived". Set the URL to:

```
{{ $('Webhook').first().json.body.results_url }}
```

Settings: **Retry On Fail: ON**, Max Tries 4, Wait 5000 ms.

## 10. Verifying Contacts — remove `"status": "completed"`

Delete that line from the JSON body. The results POST closes the run. Sent
here, it closes the run while the WhatsApp step is still working, which is the
race that made execution 135 look completed after it crashed.

## 11. The six progress nodes — never let a callback fail the search

`searching companies`, `AI Analysing`, `Domain Search`,
`Finding Decision Makers`, `Finding Emails`, `Verifying Contacts`:
Settings → **On Error: Continue**. They already retry; without this, a
tunnel hiccup on a progress POST fails the entire search.

## 12. Retry settings on the paid API nodes

| Node | Retry On Fail | Max Tries | Wait |
|---|---|---|---|
| serper | ON | 3 | 3000 ms |
| HTTP Request (DeepSeek) | ON | 3 | 3000 ms |
| every Snov.io node | ON | 3 | 2000 ms |
| WhatsApp Validator | already ON | | |

For **serper** also set **On Error: Continue**: with 16 keywords, one failed
query should not fail the other 15. `Backlisting` already skips items that
have no `organic` list.

## 13. Workflow settings

- **Error Workflow**: already set. Keep it.
- **Timeout Workflow**: ON, 30 minutes. A hung execution is then ended by n8n
  and reported through the Error Trigger, instead of waiting on the backend's
  sweeper.
