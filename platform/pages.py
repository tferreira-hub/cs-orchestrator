#!/usr/bin/env python3
"""Static HTML pages served by the CS Platform HTTP server.

These are the auth-flow screens (dev-login, signed-out, not-authorised) and the
production SSO landing page. They are pure presentation markup with no server
logic, extracted from server.py so the request-handling code stays focused and
readable. server.py imports these constants by name:

    from pages import (_DEV_LOGIN_HTML, _SIGNED_OUT_HTML,
                       _NOT_AUTHORISED_HTML, _SSO_LOGIN_HTML)

Behaviour is unchanged: the strings are byte-for-byte the same markup that
previously lived inline in server.py.
"""

from __future__ import annotations

# Local dev-login page (only served when AUTH_DEV_LOGIN=1 and Cognito is NOT
# configured). Lets you sign in as any email to exercise per-user scoping without
# a real IdP. Admin-ness comes from AUTH_ADMIN_EMAILS / CS_ADMIN_GROUPS.
_DEV_LOGIN_HTML = """<!doctype html><html><head><meta charset=utf-8>
<title>CS Platform — Sign in</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1420;color:#e8edf5;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
.card{background:#171f2e;border:1px solid #263149;border-radius:16px;padding:36px;width:360px;box-shadow:0 20px 60px rgba(0,0,0,.4)}
h1{font-size:1.3em;margin:0 0 4px}.sub{color:#8aa;font-size:.85em;margin-bottom:22px}
label{display:block;font-size:.8em;color:#9ab;margin:14px 0 6px}
input{width:100%;box-sizing:border-box;padding:11px 13px;border-radius:9px;border:1px solid #2c3854;background:#0f1626;color:#e8edf5;font-size:1em}
button{width:100%;margin-top:20px;padding:12px;border:0;border-radius:9px;background:linear-gradient(135deg,#3b82f6,#7c3aed);color:#fff;font-weight:600;font-size:1em;cursor:pointer}
.dev{margin-top:16px;font-size:.75em;color:#7788aa;text-align:center}.apps{display:flex;gap:10px;margin-bottom:20px}
.app{flex:1;text-align:center;padding:10px;border:1px solid #2c3854;border-radius:9px;font-size:.8em;color:#9ab}
.app.on{border-color:#3b82f6;color:#cfe;background:#12203a}</style></head>
<body><form class=card method=POST action="/auth/dev-login">
<h1>CS Platform</h1><div class=sub>Customer Success · sign in to your book</div>
<div class=apps><div class="app on">CS Platform</div><div class=app>JA Observe</div></div>
<label>Work email</label><input name=email type=email placeholder="you@jobadder.com" autofocus required>
<button type=submit>Sign in</button>
<div class=dev>Dev login (AUTH_DEV_LOGIN). Production uses Okta via Cognito SSO.</div>
</form></body></html>"""

# Shown after a user clicks "Sign out". The local session cookie is already cleared;
# we deliberately do NOT auto-redirect to Cognito here (a federated SAML logout bounces
# the user to the AWS Identity Center portal, and a silent /login redirect re-authenticates
# them immediately). Instead we land on a clear CS signed-out screen with an explicit
# "Sign in again" button that the user chooses to click.
_SIGNED_OUT_HTML = """<!doctype html><html><head><meta charset=utf-8>
<title>CS Platform — Signed out</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1420;color:#e8edf5;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
.card{background:#171f2e;border:1px solid #263149;border-radius:16px;padding:40px;width:360px;text-align:center;box-shadow:0 20px 60px rgba(0,0,0,.4)}
h1{font-size:1.25em;margin:0 0 6px}.sub{color:#8aa;font-size:.88em;margin-bottom:24px;line-height:1.5}
a.btn{display:block;padding:12px;border-radius:9px;background:linear-gradient(135deg,#3b82f6,#7c3aed);color:#fff;font-weight:600;font-size:1em;text-decoration:none}</style></head>
<body><div class=card>
<h1>You're signed out</h1>
<div class=sub>Your CS Platform session has ended.</div>
<a class=btn href="/login">Sign in again</a>
</div></body></html>"""

# Shown when a user authenticates successfully but is NOT entitled to the CS Platform
# (not in a CS admin or user group). Defense-in-depth behind the UI launcher.
_NOT_AUTHORISED_HTML = """<!doctype html><html><head><meta charset=utf-8>
<title>CS Platform — Access required</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1420;color:#e8edf5;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
.card{background:#171f2e;border:1px solid #263149;border-radius:16px;padding:40px;max-width:440px;text-align:center}
h1{font-size:1.25em;margin:0 0 10px}p{color:#9ab;line-height:1.5;font-size:.9em}
a{color:#8ab4ff}</style></head>
<body><div class=card>
<h1>You don't have access to the CS Platform</h1>
<p>Your JobAdder sign-in worked, but your account isn't yet a member of a Customer
Success access group. Ask your CS lead or platform admin to add you to the
<b>CS-Platform-Admins</b> group in Identity Center.</p>
<p><a href="/login?logged_out=1">Sign in as a different user</a></p>
</div></body></html>"""

# Production SSO landing page. Shown at /login when Cognito is configured, instead of
# bouncing straight to the Cognito hosted UI. SSO-only: the single action starts the
# Okta-via-Cognito OIDC flow (/login?sso=1). Two-panel layout with a data-rich CS hero
# (labelled trend line, renewal-stage bars, health gauge) and the sign-in panel. Pure
# inline SVG/CSS, no external assets, CSP-friendly. Figures are illustrative mock data.
_SSO_LOGIN_HTML = """<!doctype html><html lang=en><head><meta charset=utf-8>
<title>CS Platform, Sign in</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;min-height:100vh;background:#0a0e18;color:#eaf0f8;display:flex}
.wrap{display:flex;width:100%;min-height:100vh}
/* ---------- Hero ---------- */
.hero{flex:1.3;position:relative;overflow:hidden;padding:44px 56px;display:flex;flex-direction:column;justify-content:center;gap:26px;
background:radial-gradient(1100px 620px at 12% -8%,#1f2d4e 0,#111a30 48%,#0a0e18 100%)}
.grid{position:absolute;inset:0;opacity:.5;background:
linear-gradient(transparent 95%,rgba(96,130,195,.07) 95%) 0 0/100% 36px,
linear-gradient(90deg,transparent 95%,rgba(96,130,195,.07) 95%) 0 0/36px 100%;pointer-events:none}
.glow{position:absolute;width:520px;height:520px;border-radius:50%;filter:blur(90px);opacity:.22;pointer-events:none}
.glow.b{background:#3b82f6;top:-180px;left:-120px}.glow.p{background:#8b5cf6;bottom:-220px;right:-80px;opacity:.18}
.z{position:relative;z-index:2}
.hinner{position:relative;z-index:2;width:100%;max-width:600px;margin:0 auto;display:flex;flex-direction:column;gap:22px}
.hbrand{display:flex;align-items:center;gap:13px}
.logo{width:46px;height:46px;border-radius:12px;background:linear-gradient(135deg,#3b82f6,#8b5cf6);display:flex;align-items:center;justify-content:center;font-weight:800;color:#fff;box-shadow:0 10px 28px rgba(59,130,246,.4)}
.hbrand h1{font-size:1.12em;margin:0}.hbrand .t{color:#93a9cc;font-size:.78em;margin-top:2px}
.htag{max-width:600px}
.htag h2{font-size:2.05em;line-height:1.14;margin:0 0 12px;font-weight:720;letter-spacing:-.6px}
.htag h2 span{background:linear-gradient(120deg,#60a5fa,#a78bfa);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.htag p{color:#a6b8d6;font-size:1em;line-height:1.6;margin:0}
/* metrics strip */
.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:13px;max-width:600px}
.m{background:rgba(24,34,56,.75);border:1px solid #27375a;border-radius:14px;padding:14px 15px}
.m .k{color:#93a9cc;font-size:.67em;text-transform:uppercase;letter-spacing:.7px}
.m .v{font-size:1.5em;font-weight:720;margin-top:5px;letter-spacing:-.5px}
.m .d{font-size:.73em;margin-top:4px;display:flex;align-items:center;gap:4px}.up{color:#34d399}.down{color:#fb7185}.flat{color:#93a9cc}
/* dashboard grid */
.dash{display:grid;grid-template-columns:1.5fr 1fr;gap:14px;max-width:600px}
.panelcard{background:rgba(24,34,56,.62);border:1px solid #27375a;border-radius:16px;padding:15px 17px 11px}
.pt{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px}
.pt b{font-size:.88em}.pt span{color:#93a9cc;font-size:.71em}
.axis{fill:#6f84a6;font-size:9px;font-family:system-ui}
.legend{display:flex;gap:14px;margin-top:8px;font-size:.72em;color:#a6b8d6}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px;vertical-align:middle}
.span2{grid-column:1 / -1}
.foot{color:#6f84a6;font-size:.76em;display:flex;align-items:center;gap:8px;max-width:600px}
.foot b{color:#8fa6c9;font-weight:600}
/* ---------- Sign-in panel ---------- */
.side{flex:1;display:flex;align-items:center;justify-content:center;padding:40px;background:#0c1120;border-left:1px solid #1a2238}
.card{width:100%;max-width:380px}
.pb{display:flex;align-items:center;gap:11px;margin-bottom:30px}
.pb .lg{width:38px;height:38px;border-radius:10px;background:linear-gradient(135deg,#3b82f6,#8b5cf6);display:flex;align-items:center;justify-content:center;font-weight:800;color:#fff}
.pb .nm{font-size:.82em;color:#93a9cc}
.card h3{font-size:1.5em;margin:0 0 8px;letter-spacing:-.3px}
.sub{color:#a6b8d6;font-size:.9em;line-height:1.55;margin-bottom:26px}
a.sso{display:flex;align-items:center;justify-content:center;gap:10px;width:100%;padding:14px;border-radius:12px;background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:#fff;font-weight:650;font-size:1.02em;text-decoration:none;transition:filter .15s,transform .08s,box-shadow .15s;box-shadow:0 10px 26px rgba(79,110,220,.35)}
a.sso:hover{filter:brightness(1.08);box-shadow:0 14px 34px rgba(79,110,220,.45)}a.sso:active{transform:translateY(1px)}
.sso svg{width:18px;height:18px}
.feat{margin-top:24px;display:flex;flex-direction:column;gap:11px}
.feat .f{display:flex;gap:11px;align-items:flex-start;font-size:.85em;color:#b7c6e0}
.feat .f svg{width:17px;height:17px;color:#60a5fa;flex:none;margin-top:1px}
.jane{margin-top:22px;display:flex;gap:12px;align-items:flex-start;padding:14px 15px;border-radius:14px;background:linear-gradient(135deg,rgba(59,130,246,.14),rgba(139,92,246,.14));border:1px solid #2d3f66}
.jane .av{width:38px;height:38px;border-radius:50%;flex:none;background:linear-gradient(135deg,#3b82f6,#8b5cf6);display:flex;align-items:center;justify-content:center;color:#fff;font-weight:800;font-size:.95em;box-shadow:0 6px 16px rgba(79,110,220,.4)}
.jane .jt{font-size:.9em;color:#eaf0f8}.jane .jt b{color:#fff}
.jane .jt .r{display:block;color:#9fb3d1;font-size:.86em;margin-top:3px;line-height:1.45}
.jane .chip{display:inline-block;font-size:.64em;font-weight:700;letter-spacing:.5px;text-transform:uppercase;color:#c7b8ff;background:rgba(139,92,246,.2);border:1px solid #4c3f80;border-radius:999px;padding:2px 7px;margin-left:6px;vertical-align:middle}
.err{background:#3a1420;border:1px solid #7f1d2e;color:#fecdd6;border-radius:10px;padding:10px 12px;font-size:.82em;margin-bottom:20px}
@media(max-width:900px){.hero{display:none}.side{flex:1;border-left:0}}
</style></head>
<body><div class=wrap>
  <section class=hero>
    <div class=grid></div><div class="glow b"></div><div class="glow p"></div>
    <div class=hinner>
    <div class="hbrand z"><div class=logo>CS</div><div><h1>CS Platform</h1><div class=t>Customer Success at JobAdder</div></div></div>
    <div class="htag z">
      <h2>Your whole book,<br><span>one prioritised view</span></h2>
      <p>Portfolio health, renewal cadence, churn risk and expansion signals, pulled together and ranked so you act on what matters first.</p>

      <div class=metrics>
        <div class=m><div class=k>Net Revenue Retention</div><div class=v>112%</div><div class="d up">&#9650; 4.2 pts vs last quarter</div></div>
        <div class=m><div class=k>Accounts at risk</div><div class=v>7</div><div class="d down">&#9650; 2 new this week</div></div>
        <div class=m><div class=k>Renewals in 120 days</div><div class=v>23</div><div class="d flat">&#8226; $4.8M ARR</div></div>
      </div>

      <div class="dash">
        <!-- NRR trend line with axes + value labels -->
        <div class="panelcard">
          <div class="pt"><b>Net revenue retention</b><span>Apr to Sep</span></div>
          <svg viewBox="0 0 300 150" width="100%" height="134" role="img" aria-label="NRR trend from 103 to 112 percent">
            <defs><linearGradient id="ln" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#60a5fa" stop-opacity="0.4"/><stop offset="100%" stop-color="#60a5fa" stop-opacity="0"/></linearGradient></defs>
            <g>
              <line x1="34" y1="16" x2="292" y2="16" stroke="#223150"/><text x="28" y="20" text-anchor="end" class="axis">115</text>
              <line x1="34" y1="56" x2="292" y2="56" stroke="#223150"/><text x="28" y="60" text-anchor="end" class="axis">110</text>
              <line x1="34" y1="96" x2="292" y2="96" stroke="#223150"/><text x="28" y="100" text-anchor="end" class="axis">105</text>
              <line x1="34" y1="120" x2="292" y2="120" stroke="#223150"/><text x="28" y="124" text-anchor="end" class="axis">100</text>
            </g>
            <path d="M46,96 L96,80 L146,88 L196,56 L246,40 L288,24 L288,128 L46,128 Z" fill="url(#ln)"/>
            <path d="M46,96 L96,80 L146,88 L196,56 L246,40 L288,24" fill="none" stroke="#60a5fa" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
            <g fill="#a78bfa">
              <circle cx="46" cy="96" r="3"/><circle cx="96" cy="80" r="3"/><circle cx="146" cy="88" r="3"/><circle cx="196" cy="56" r="3"/><circle cx="246" cy="40" r="3"/><circle cx="288" cy="24" r="4" fill="#60a5fa"/>
            </g>
            <text x="282" y="18" text-anchor="end" fill="#cfe0ff" font-size="11" font-weight="700">112%</text>
            <g class="axis" text-anchor="middle">
              <text x="46" y="146">Apr</text><text x="96" y="146">May</text><text x="146" y="146">Jun</text><text x="196" y="146">Jul</text><text x="246" y="146">Aug</text><text x="288" y="146">Sep</text>
            </g>
          </svg>
        </div>
        <!-- health donut: pathLength=100 so segments are plain percentages (68/22/10) -->
        <div class="panelcard">
          <div class="pt"><b>Health mix</b><span>184 accounts</span></div>
          <svg viewBox="0 0 140 128" width="100%" height="128" role="img" aria-label="Health: 68 percent healthy, 22 percent watch, 10 percent at risk">
            <g transform="translate(70,60) rotate(-90)">
              <circle r="46" fill="none" stroke="#1b2742" stroke-width="16"/>
              <circle r="46" fill="none" stroke="#34d399" stroke-width="16" pathLength="100" stroke-dasharray="68 32" stroke-dashoffset="0"/>
              <circle r="46" fill="none" stroke="#fbbf24" stroke-width="16" pathLength="100" stroke-dasharray="22 78" stroke-dashoffset="-68"/>
              <circle r="46" fill="none" stroke="#fb7185" stroke-width="16" pathLength="100" stroke-dasharray="10 90" stroke-dashoffset="-90"/>
            </g>
            <text x="70" y="56" text-anchor="middle" fill="#eaf0f8" font-size="22" font-weight="750">68%</text>
            <text x="70" y="74" text-anchor="middle" fill="#93a9cc" font-size="10">healthy</text>
          </svg>
          <div class="legend" style="justify-content:center;flex-wrap:wrap;gap:12px"><span><i style="background:#34d399"></i>Healthy 125</span><span><i style="background:#fbbf24"></i>Watch 40</span><span><i style="background:#fb7185"></i>Risk 19</span></div>
        </div>
        <!-- renewal-stage bars -->
        <div class="panelcard span2">
          <div class="pt"><b>Open renewals by stage</b><span>next 120 days</span></div>
          <svg viewBox="0 0 520 96" width="100%" height="86" role="img" aria-label="Renewals: T-120 nine, T-90 six, T-60 five, T-30 three">
            <line x1="24" y1="70" x2="512" y2="70" stroke="#223150"/>
            <g text-anchor="middle">
              <rect x="44" y="16" width="78" height="54" rx="6" fill="#3b82f6"/><text x="83" y="11" fill="#cfe0ff" font-size="12" font-weight="700">9</text>
              <rect x="178" y="34" width="78" height="36" rx="6" fill="#6366f1"/><text x="217" y="29" fill="#cfe0ff" font-size="12" font-weight="700">6</text>
              <rect x="312" y="40" width="78" height="30" rx="6" fill="#8b5cf6"/><text x="351" y="35" fill="#cfe0ff" font-size="12" font-weight="700">5</text>
              <rect x="446" y="52" width="78" height="18" rx="6" fill="#a78bfa"/><text x="485" y="47" fill="#cfe0ff" font-size="12" font-weight="700">3</text>
              <g class="axis"><text x="83" y="88">T-120</text><text x="217" y="88">T-90</text><text x="351" y="88">T-60</text><text x="485" y="88">T-30</text></g>
            </g>
          </svg>
        </div>
      </div>
    </div>
    <div class="foot z"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#34d399" stroke-width="2.4"><path d="M12 2l8 4v6c0 5-3.5 8-8 10-4.5-2-8-5-8-10V6z"/></svg> <b>Secure access.</b> Single sign-on via Okta. JobAdder internal.</div>
    </div>
  </section>

  <section class=side>
    <div class=card>
      <div class=pb><div class=lg>CS</div><div class=nm>Customer Success Platform</div></div>
      __ERROR__
      <h3>Welcome back</h3>
      <div class=sub>Access is restricted to JobAdder staff. Sign in with your company account to open your Customer Success workspace.</div>
      <a class=sso href="/login?sso=1">
        <svg viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=2 stroke-linecap=round stroke-linejoin=round><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><polyline points="10 17 15 12 10 7"/><line x1=15 y1=12 x2=3 y2=12/></svg>
        Sign in with JobAdder SSO</a>
      <div class=feat>
        <div class=f><svg viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=2><path d="M3 3v18h18"/><path d="M7 14l4-4 3 3 5-6"/></svg><div>One prioritised queue across your whole portfolio.</div></div>
        <div class=f><svg viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=2><path d="M12 2l8 4v6c0 5-3.5 8-8 10-4.5-2-8-5-8-10V6z"/></svg><div>Churn risk and renewal cadence, surfaced early.</div></div>
        <div class=f><svg viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=2><circle cx=12 cy=12 r=9/><path d="M12 7v5l3 2"/></svg><div>Drafted outreach ready, grounded in real signals.</div></div>
      </div>
      <div class=jane>
        <div class=av>J</div>
        <div class=jt><b>Meet Jane</b><span class=chip>AI</span>
          <span class=r>Your Customer Success specialist. Ask her what the signals mean and the smartest next move, always grounded in real evidence.</span>
        </div>
      </div>
    </div>
  </section>
</div></body></html>"""
