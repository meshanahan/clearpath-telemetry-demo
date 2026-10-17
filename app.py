"""ClearPath synthetic order telemetry API. No real applicant data."""
import os, sqlite3, time, secrets
import logging
from pathlib import Path
from threading import RLock
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST

try:
 import sentry_sdk
 if os.getenv("SENTRY_DSN"):
  sentry_sdk.init(dsn=os.environ["SENTRY_DSN"], traces_sample_rate=1.0, environment="clearpath-demo")
except ImportError:
 pass

app = FastAPI(title="ClearPath Synthetic Screening API", version="0.3.0")
app.add_middleware(CORSMiddleware, allow_origins=[x.strip() for x in os.getenv("CORS_ORIGINS","*").split(",") if x.strip()], allow_credentials=False, allow_methods=["GET","POST","OPTIONS"], allow_headers=["X-Demo-Key","Content-Type"])
DB = Path(os.getenv("CLEARPATH_DB","/tmp/clearpath.db"))
LOCK = RLock()
CUSTOMERS = ["Northstar Logistics","Meridian Health Group","Summit Retail Holdings","Harbor Financial Services"]
ORDER_GAUGE = Gauge("clearpath_orders","Orders by internal status",["status"])
WEBHOOK_ERRORS = Counter("clearpath_webhook_errors_total","Webhook and warning events",["reason"])
PROVIDER_ERRORS = Counter("clearpath_provider_errors_total","Provider auth failures",["reason"])
SCENARIOS = Counter("clearpath_scenarios_triggered_total","Scenario triggers",["scenario"])
LATENCY = Histogram("clearpath_order_operation_seconds","Scenario operation duration",["operation"])

def connect():
 c=sqlite3.connect(DB)
 c.row_factory=sqlite3.Row
 return c

def refresh():
 with connect() as c:
  rows={r["internal_status"]:r["n"] for r in c.execute("SELECT internal_status,COUNT(*) n FROM orders GROUP BY internal_status")}
 for status in ("COMPLETED","IN_PROGRESS","AWAITING_EXTERNAL"):
  ORDER_GAUGE.labels(status).set(rows.get(status,0))

def seed():
 with LOCK:
  with connect() as c:
   c.execute("CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY,customer TEXT,internal_status TEXT,ats_status TEXT,verification_stage TEXT,updated_at REAL)")
   if c.execute("SELECT COUNT(*) FROM orders").fetchone()[0]==0:
    now=time.time()
    for i in range(1,51):
     status="COMPLETED" if i<=30 else "IN_PROGRESS"
     c.execute("INSERT INTO orders VALUES (?,?,?,?,?,?)",(f"CP-ORD-{300000+i}",CUSTOMERS[(i-1)%4],status,status,"RESULT_ASSEMBLY" if i<=30 else "VERIFICATION",now))
  refresh()

@app.on_event("startup")
def startup(): seed()

@app.get("/health")
def health(): return {"status":"ok","mode":"synthetic","telemetry":"application-generated","service":"clearpath-telemetry-api"}

@app.get("/orders")
def orders():
 with connect() as c: return [dict(r) for r in c.execute("SELECT * FROM orders ORDER BY id")]

@app.get("/orders/{order_id}")
def order(order_id:str):
 with connect() as c: r=c.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
 if r is None: raise HTTPException(404,"Synthetic order not found")
 return dict(r)

@app.get("/summary")
def summary():
 with connect() as c:
  total=c.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
  drift=c.execute("SELECT COUNT(*) FROM orders WHERE internal_status='COMPLETED' AND ats_status='PENDING'").fetchone()[0]
  waiting=c.execute("SELECT COUNT(*) FROM orders WHERE internal_status='AWAITING_EXTERNAL'").fetchone()[0]
  customers=[dict(r) for r in c.execute("SELECT customer,COUNT(*) orders FROM orders WHERE internal_status='COMPLETED' AND ats_status='PENDING' GROUP BY customer")]
 return {"mode":"synthetic","total_orders":total,"ats_drift_orders":drift,"awaiting_employer":waiting,"ats_drift_customers":customers,"generated_at":time.time()}

@app.get("/metrics")
def metrics(): return Response(generate_latest(),media_type=CONTENT_TYPE_LATEST)

def authorize(key):
 expected=os.getenv("DEMO_ADMIN_KEY")
 if not expected or not key or not secrets.compare_digest(key,expected): raise HTTPException(403,"Admin key required")

@app.post("/demo/{scenario}")
def trigger(scenario:str,x_demo_key:str|None=Header(default=None)):
 authorize(x_demo_key)
 if scenario not in ("ats-drift","provider-401","employer-delay","telemetry-noise"): raise HTTPException(404,"Unknown scenario")
 changed_orders=None
 with LATENCY.labels(scenario).time():
  with LOCK:
   with connect() as c:
    if scenario=="ats-drift":
     result=c.execute("UPDATE orders SET ats_status='PENDING',updated_at=? WHERE id IN (SELECT id FROM orders WHERE internal_status='COMPLETED' AND ats_status!='PENDING' ORDER BY id LIMIT 16)",(time.time(),))
     changed_orders=result.rowcount
     if changed_orders:
      WEBHOOK_ERRORS.labels("semantic_ack_mismatch").inc(changed_orders)
    elif scenario=="employer-delay":
     c.execute("UPDATE orders SET internal_status='AWAITING_EXTERNAL',verification_stage='EMPLOYER_RESPONSE',updated_at=? WHERE id IN (SELECT id FROM orders WHERE internal_status='IN_PROGRESS' ORDER BY id LIMIT 18)",(time.time(),))
    elif scenario=="provider-401": PROVIDER_ERRORS.labels("token_expired").inc(8)
    else: WEBHOOK_ERRORS.labels("non_customer_telemetry_warning").inc(3)
   refresh()
  if scenario!="ats-drift" or changed_orders:
   SCENARIOS.labels(scenario).inc()
  if os.getenv("SENTRY_DSN") and (scenario!="ats-drift" or changed_orders):
   import sentry_sdk
   with sentry_sdk.push_scope() as scope:
    scope.set_tag("demo.synthetic", "true")
    scope.set_tag("clearpath.scenario", scenario)
    invocation_id=secrets.token_hex(8)
    scope.set_context("clearpath_incident", {"scenario":scenario,"synthetic":True,"invocation_id":invocation_id,"changed_orders":changed_orders,"summary":summary()})
    if scenario=="provider-401":
     try:
      raise RuntimeError("Synthetic provider token rejected: HTTP 401 (demo injection)")
     except RuntimeError as error:
      sentry_sdk.capture_exception(error)
    elif scenario=="ats-drift":
     sentry_sdk.capture_message(f"Synthetic ATS webhook semantic ACK mismatch: {changed_orders} orders", level="error")
    elif scenario=="employer-delay":
     sentry_sdk.capture_message("Synthetic employer-response backlog: external dependency", level="warning")
    else:
     sentry_sdk.capture_message("Synthetic non-customer telemetry warning", level="warning")
 if scenario=="ats-drift":
  return {"scenario":scenario,"result":"synthetic scenario triggered" if changed_orders else "no-op","changed_orders":changed_orders,"no_op":changed_orders==0,"summary":summary()}
 return {"scenario":scenario,"result":"synthetic scenario triggered","summary":summary()}

@app.post("/demo/reset")
def reset(x_demo_key:str|None=Header(default=None)):
 authorize(x_demo_key)
 with LOCK:
  with connect() as c: c.execute("DELETE FROM orders")
  seed()
 return {"result":"synthetic orders reset","note":"Prometheus counters are cumulative until process restart"}
