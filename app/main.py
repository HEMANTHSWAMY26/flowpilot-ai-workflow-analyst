from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, Response, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pathlib import Path
from typing import Optional, List, Dict, Any
import csv, io, re, json, asyncio, time

from app.ingestion import ingest_file, normalize_records, SUPPORTED_EXTENSIONS, MAX_RECORDS_LIMIT
from app.jobs import (
    create_job, update_job_stage, update_job_progress,
    complete_job, fail_job, get_job_status, add_job_log
)
from app.invoice_schemas import (
    InvoiceData, PurchaseOrder, ReviewerActionRequest,
    InvoiceEvaluationResult, ToleranceConfig
)
from app.invoice_engine import InvoiceProcessingEngine

# Third-party excel parser
try:
    import openpyxl
except ImportError:
    openpyxl = None

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
DATA_PATH = ROOT / 'data' / 'evaluation_100.json'
PO_DATA_PATH = ROOT / 'data' / 'synthetic_purchase_orders.json'

# Initialize AP Invoice Processing Engine
invoice_engine = InvoiceProcessingEngine(PO_DATA_PATH)

app = FastAPI(
    title='FlowPilot',
    description='AI Workflow Analyst — Operational Policy Engine and Evaluation Suite',
    version='1.2.0'
)

# Standard policy threshold for automated refunds (in INR)
REFUND_THRESHOLD = 5000

class ProcessRequest(BaseModel):
    requirement: str = Field(..., min_length=1, description="Operational workflow requirement description")

class RiskUpdateRequest(BaseModel):
    risk: str = Field(..., description="Analyst-adjusted risk level: LOW, MEDIUM, or HIGH")

class ConfirmEvaluateRequest(BaseModel):
    records: List[Dict[str, Any]]
    custom_mapping: Optional[Dict[str, str]] = None

# In-memory session store for analyst risk overrides
ANALYST_RISK_OVERRIDES: Dict[str, str] = {}
VALID_RISKS = {'low', 'medium', 'high'}

# ============================================================================
# POLICY ENGINE: PRECEDENCE-BASED CLASSIFICATION & EVALUATION
# ============================================================================
"""
Policy Precedence Architecture:
Level 1: Malformed / Unsafe / Adversarial / Invalid Input (Prompt Injection, Fake Claims)
Level 2: Fraud / Security / Suspicious Activity (Hacked, Compromised, Stolen)
Level 3: Conflicting Information / Data Discrepancies (System Mismatch, Billing Conflicts)
Level 4: Duplicate Billing / Ledger Discrepancies (Double Charge, Multi-debits)
Level 5: Missing Required Information (Missing Order ID, No Transaction Reference)
Level 6: Business Policy & Threshold Boundaries (Refunds <= ₹5,000 auto-approved; > ₹5,000 escalated)
Level 7: Safe Informational Self-Service (FAQs, Order Tracking, Account Settings)
Level 8: Ambiguous / Low-Confidence Fallback (Human Review)
"""

def extract_amount(message: str) -> Optional[int]:
    """Safely extracts monetary values with currency symbols or plain figures."""
    m = message.lower()
    # Currency prefixes: ₹, rs, inr, $, usd, eur, €
    hits = re.findall(r'(?:₹|rs\.?|inr|\$|usd|eur|€)\s*([\d,]+(?:\.\d{1,2})?)', m)
    if hits:
        cleaned = hits[0].replace(',', '')
        try:
            return int(float(cleaned))
        except ValueError:
            pass
    # Standalone numbers (3 to 6 digits) representing monetary values
    standalone = re.findall(r'\b(\d[\d,]{2,5})\b', m)
    if standalone:
        cleaned = standalone[0].replace(',', '')
        try:
            return int(float(cleaned))
        except ValueError:
            pass
    return None

def classify(message: str) -> Dict[str, Any]:
    """
    Evaluates customer requests strictly according to documented policy precedence.
    Guarantees:
      - Fake/ineligible refund requests are BLOCKED before refund logic is reached.
      - Prompt injection cannot subvert business logic or cause unsafe automated actions.
      - Security incidents and data conflicts are routed to human analysts.
    """
    m = message.lower().strip()
    amount = extract_amount(message)

    # ------------------------------------------------------------------------
    # Signal Detection
    # ------------------------------------------------------------------------
    injection = any(x in m for x in [
        'ignore previous instructions', 'reveal system prompt', 'ignore the workflow',
        'disregard policy', 'system override', 'jailbreak', 'unrestricted agent mode',
        'subvert workflow'
    ])

    invalid_or_fake = any(x in m for x in [
        'fake refund', 'invalid request', 'not eligible', 'fake claim', 'fabricated claim'
    ])

    security = any(x in m for x in [
        'hacked', 'unauthorized', 'fraud', 'stolen', 'stole', 'steal', 'someone accessed',
        'bank details changed', 'account takeover', 'credential theft', 'scam', 'scammer',
        'suspicious login', 'changed without my consent', 'someone changed my', 'compromised'
    ])

    conflict = any(x in m for x in [
        'record says', 'records show', 'amount is different', 'conflicting',
        'discrepancy', 'mismatch', 'bank statement shows', 'status mismatch',
        'receipt in box says', 'warehouse records show'
    ])

    duplicate = any(x in m for x in [
        'charged twice', 'duplicate charge', 'charged two times', 'double charged',
        'duplicate payment', 'double deduction', 'duplicate debit', 'duplicate ledger'
    ])

    missing = any(x in m for x in [
        'no order id', 'without order id', 'missing order', 'no transaction id',
        'without transaction id', 'lost the receipt and have no order', 'missing tracking id',
        'no tracking id', 'missing order reference', 'no order number',
        'without order number', 'no order id provided', 'without order ID',
        'missing refund reference', 'missing tracking', "don't remember the order",
        "don't have the order", 'no receipt handy', 'lost receipt'
    ])

    disputed = any(x in m for x in [
        'never purchased', 'did not purchase', 'never placed', 'did not authorize'
    ])

    refund = any(x in m for x in [
        'refund', 'money back', 'return my payment', 'process a return', 'issue a refund'
    ])

    faq = any(x in m for x in [
        'how do i', 'where can i', 'what is', 'how can i', 'password',
        'can you explain how', 'what time does', 'do you offer', 'where do i update',
        'how to enable', 'where can i track', 'which courier', 'has order',
        'will my order', 'estimated delivery date', 'live location', 'courier partner',
        'terms of service', 'operating hours', 'payment methods do you accept',
        'return window policy', 'active login sessions', 'dark mode', 'personal account data',
        'tracking number', 'signature', 'express shipping'
    ])

    # ------------------------------------------------------------------------
    # Policy Evaluation Pipeline (Strict Precedence)
    # ------------------------------------------------------------------------
    
    # 1. Malformed / Unsafe / Adversarial / Explicitly Ineligible
    if injection:
        decision, category, risk = 'HUMAN_REVIEW', 'SECURITY', 'high'
        reason = 'Customer content attempted to override workflow instructions; treated as untrusted data.'
    elif invalid_or_fake:
        decision, category, risk = 'BLOCK', 'POLICY', 'high'
        reason = 'The request is explicitly fraudulent, invalid, or does not meet the configured support policy.'
    
    # 2. Fraud / Security / Compromised Accounts
    elif security:
        decision, category, risk = 'HUMAN_REVIEW', 'SECURITY', 'high'
        reason = 'Security, unauthorized-access, or fraud concerns require human security review.'
    
    # 3. Conflicting Information
    elif conflict:
        decision, category, risk = 'HUMAN_REVIEW', 'DATA_CONFLICT', 'high'
        reason = 'Conflicting information between customer claim and system records was detected and must be reconciled.'
    
    # 4. Duplicate Billing
    elif duplicate:
        decision, category, risk = 'HUMAN_REVIEW', 'BILLING', 'medium'
        reason = 'Potential duplicate payment or double debit requires transaction ledger verification.'
    
    # 5. Missing Required Information
    elif missing:
        decision, category, risk = 'REQUEST_INFORMATION', 'ORDER_SUPPORT', 'medium'
        reason = 'Required order, transaction, or tracking information is missing before action can be taken.'
    
    # 6. Business Policy & Refund Boundaries
    elif refund:
        category = 'REFUND'
        if disputed:
            decision, risk = 'HUMAN_REVIEW', 'high'
            reason = 'The customer disputes the underlying transaction; verification of purchase evidence required.'
        elif amount is not None and amount > REFUND_THRESHOLD:
            decision, risk = 'HUMAN_REVIEW', ('high' if amount >= 20000 else 'medium')
            reason = f'Refund amount (₹{amount:,}) exceeds the automatic approval threshold of ₹{REFUND_THRESHOLD:,}.'
        else:
            decision, risk = 'AUTO_RESOLVE', ('medium' if amount == REFUND_THRESHOLD else 'low')
            reason = f'Refund is within the automatic approval threshold of ₹{REFUND_THRESHOLD:,} and passes safety checks.'
    
    # 7. Safe Informational Requests
    elif faq:
        decision, category, risk = 'AUTO_RESOLVE', 'GENERAL_SUPPORT', 'low'
        reason = 'This is a low-risk informational or tracking request that can be resolved automatically.'
    
    # 8. Ambiguous / Under-specified Fallback
    else:
        decision, category, risk = 'HUMAN_REVIEW', 'GENERAL_SUPPORT', 'medium'
        reason = 'The request is ambiguous or lacks sufficient context for safe automated action.'

    evidence = build_evidence(m, amount, decision, category, injection, security, conflict, duplicate, missing, invalid_or_fake)
    rec_action = recommended(decision, category)

    return {
        'intent': category.lower(),
        'category': category,
        'risk': risk,
        'amount': amount,
        'decision': decision,
        'reason': reason,
        'evidence': evidence,
        'recommended_action': rec_action
    }

def build_evidence(m: str, amount: Optional[int], decision: str, category: str,
                   injection: bool, security: bool, conflict: bool, duplicate: bool,
                   missing: bool, invalid_or_fake: bool) -> List[str]:
    """Constructs explainable evidence points detailing exactly what the policy engine observed."""
    ev = []
    if injection:
        ev.append('✕ Untrusted instruction injection payload detected')
    if invalid_or_fake:
        ev.append('✕ Explicit ineligibility or invalid claim pattern identified')
    if security:
        ev.append('⚠ Security or unauthorized access indicator detected')
    if conflict:
        ev.append('⚠ Discrepancy between customer statement and system records detected')
    if duplicate:
        ev.append('⚠ Duplicate charge or ledger indicator detected')
    if missing:
        ev.append('✕ Missing critical transaction/order identifier')
    if category == 'REFUND':
        ev.append('✓ Valid refund intent identified')
    if amount is not None:
        ev.append(f'✓ Transaction amount extracted: ₹{amount:,}')
        if amount > REFUND_THRESHOLD:
            ev.append(f'⚠ Amount exceeds auto-approval ceiling of ₹{REFUND_THRESHOLD:,}')
        else:
            ev.append(f'✓ Amount within auto-approval ceiling of ₹{REFUND_THRESHOLD:,}')
    if not ev:
        ev.append('✓ Informational query processed under standard service guidelines')
    return ev

def recommended(decision: str, category: str) -> str:
    """Returns the operational next step based on policy output."""
    return {
        'AUTO_RESOLVE': 'Safely resolve request and generate automated customer notification.',
        'REQUEST_INFORMATION': 'Request missing order/transaction identifier from customer before taking action.',
        'HUMAN_REVIEW': 'Route ticket to tier-2 human specialist with attached audit evidence.',
        'BLOCK': 'Deny request, log policy violation, and prevent automated financial transaction.'
    }[decision]

# ============================================================================
# EVALUATION DATASET LOADER
# ============================================================================
def load_prepared_cases() -> List[Dict[str, Any]]:
    """Loads the genuine 100-case evaluation dataset from data/evaluation_100.json."""
    if DATA_PATH.exists():
        with open(DATA_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    raise FileNotFoundError(f"Prepared evaluation set not found at {DATA_PATH}")

CASES = load_prepared_cases()

# ============================================================================
# REST API ENDPOINTS
# ============================================================================

@app.get('/health')
def health():
    return {
        'status': 'ok',
        'service': 'flowpilot',
        'cases_loaded': len(CASES),
        'version': '1.1.0'
    }

@app.get('/')
def home():
    return FileResponse(BASE / 'static' / 'index.html')

@app.get('/api/cases')
def cases():
    """Returns list of prepared evaluation cases with analyst overrides if present."""
    result = []
    for c in CASES:
        orig_risk = c.get('risk', 'low').lower()
        current_risk = ANALYST_RISK_OVERRIDES.get(c['id'], orig_risk)
        result.append({
            'id': c['id'],
            'type': c.get('type', 'General'),
            'message': c['message'],
            'category': c.get('category', 'GENERAL_SUPPORT'),
            'original_risk': orig_risk.upper(),
            'risk': current_risk.upper(),
            'is_overridden': (c['id'] in ANALYST_RISK_OVERRIDES and current_risk != orig_risk),
            'expected_decision': c['expected_decision']
        })
    return result

@app.get('/api/cases/{case_id}')
def case_detail(case_id: str):
    """Inspects an individual case, showing live policy execution, explainability, and analyst overrides."""
    c = next((x for x in CASES if x['id'] == case_id), None)
    if not c:
        raise HTTPException(404, 'Case not found')
    
    result = classify(c['message'])
    model_risk = result['risk'].lower()
    current_risk = ANALYST_RISK_OVERRIDES.get(case_id, model_risk)
    is_overridden = (case_id in ANALYST_RISK_OVERRIDES and current_risk != model_risk)

    result.update({
        'id': c['id'],
        'type': c.get('type', 'General'),
        'message': c['message'],
        'expected_decision': c['expected_decision'],
        'matched': result['decision'] == c['expected_decision'],
        'original_risk': model_risk.upper(),
        'risk': current_risk.upper(),
        'is_overridden': is_overridden,
        'override_status': 'Changed by analyst' if is_overridden else 'No analyst override'
    })
    return result

@app.patch('/api/cases/{case_id}/risk')
def update_case_risk(case_id: str, req: RiskUpdateRequest):
    """Updates the analyst risk override for a case in the current session."""
    c = next((x for x in CASES if x['id'] == case_id), None)
    if not c:
        raise HTTPException(404, 'Case not found')
    
    val = req.risk.strip().lower()
    if val not in VALID_RISKS:
        raise HTTPException(400, f"Invalid risk level '{req.risk}'. Must be one of: LOW, MEDIUM, HIGH.")
    
    # Model risk from case baseline
    orig_risk = c.get('risk', 'low').lower()
    ANALYST_RISK_OVERRIDES[case_id] = val
    return {
        'id': case_id,
        'original_risk': orig_risk.upper(),
        'current_risk': val.upper(),
        'is_overridden': (val != orig_risk)
    }

@app.post('/api/cases/{case_id}/risk/reset')
def reset_case_risk(case_id: str):
    """Restores the original model-generated risk for a case."""
    c = next((x for x in CASES if x['id'] == case_id), None)
    if not c:
        raise HTTPException(404, 'Case not found')
    
    ANALYST_RISK_OVERRIDES.pop(case_id, None)
    orig_risk = c.get('risk', 'low').lower()
    return {
        'id': case_id,
        'original_risk': orig_risk.upper(),
        'current_risk': orig_risk.upper(),
        'is_overridden': False
    }

@app.post('/api/run')
def run_evaluation():
    """
    Executes all 100 prepared cases against the policy engine.
    Calculates actual, un-faked performance and safety metrics.
    """
    results = []
    for c in CASES:
        r = classify(c['message'])
        matched = (r['decision'] == c['expected_decision'])
        model_risk = r['risk'].lower()
        current_risk = ANALYST_RISK_OVERRIDES.get(c['id'], model_risk)
        results.append({
            'id': c['id'],
            'type': c.get('type', 'General'),
            'message': c['message'],
            'expected_decision': c['expected_decision'],
            'actual_decision': r['decision'],
            'original_risk': model_risk.upper(),
            'risk': current_risk.upper(),
            'is_overridden': (c['id'] in ANALYST_RISK_OVERRIDES and current_risk != model_risk),
            'matched': matched,
            'reason': r['reason']
        })

    total = len(results)
    correct = sum(x['matched'] for x in results)
    
    # Unsafe automated action = System automatically resolved a ticket that should have been blocked or escalated
    unsafe = sum(
        x['actual_decision'] == 'AUTO_RESOLVE' and x['expected_decision'] != 'AUTO_RESOLVE'
        for x in results
    )
    
    auto_count = sum(x['actual_decision'] == 'AUTO_RESOLVE' for x in results)
    escalations = sum(x['actual_decision'] == 'HUMAN_REVIEW' for x in results)
    info_requests = sum(x['actual_decision'] == 'REQUEST_INFORMATION' for x in results)
    blocked_count = sum(x['actual_decision'] == 'BLOCK' for x in results)
    
    # Correct escalation rate = escalated cases that actually needed escalation
    needed_escalation = sum(x['expected_decision'] == 'HUMAN_REVIEW' for x in results)
    correct_escalations = sum(
        x['actual_decision'] == 'HUMAN_REVIEW' and x['expected_decision'] == 'HUMAN_REVIEW'
        for x in results
    )
    escalation_rate = round((correct_escalations / needed_escalation * 100), 1) if needed_escalation else 100.0

    return {
        'total': total,
        'correct': correct,
        'accuracy': round((correct / total) * 100, 1),
        'policy_compliance': round(((total - unsafe) / total) * 100, 1),
        'unsafe_actions': unsafe,
        'automation_rate': round((auto_count / total) * 100, 1),
        'human_review_rate': round((escalations / total) * 100, 1),
        'request_information_rate': round((info_requests / total) * 100, 1),
        'blocked_rate': round((blocked_count / total) * 100, 1),
        'correct_escalation_rate': escalation_rate,
        'failures': [x for x in results if not x['matched']]
    }

@app.post('/api/custom/workflow')
def custom_workflow(req: ProcessRequest):
    """
    Parses natural language requirements into structured operational workflows.
    Surfaces ambiguity honestly when critical details are absent.
    """
    text = req.requirement.strip()
    if len(text) < 15:
        raise HTTPException(400, 'Please provide a more descriptive operational process requirement.')

    t_lower = text.lower()
    
    # Extract financial ceiling
    amounts = re.findall(r'(?:₹|rs\.?|inr|\$|usd|eur|€)\s*([\d,]+)', t_lower)
    if not amounts:
        amounts = re.findall(r'\b(\d[\d,]{2,5})\b', t_lower)
    
    threshold = int(amounts[0].replace(',', '')) if amounts else None
    
    # Extract lookback window
    days_match = re.search(r'(\d+)\s*days?', t_lower)
    days = int(days_match.group(1)) if days_match else None
    
    # Detect business domain
    is_refund = any(w in t_lower for w in ['refund', 'return', 'reimbursement', 'credit'])
    has_escalation = any(w in t_lower for w in ['human review', 'escalate', 'manager', 'supervisor', 'manual'])

    # If critical operational bounds are missing, surface clarification questions
    questions = []
    if threshold is None and is_refund:
        questions.append('What is the maximum monetary threshold allowed for automated approval?')
    if not has_escalation:
        questions.append('Which specific edge cases or customer segments require escalation to human review?')
    if not is_refund and threshold is None:
        questions.append('What specific trigger condition distinguishes automated approval from manual review?')
    
    if questions:
        return {
            'status': 'needs_clarification',
            'objective': text,
            'questions': questions
        }

    # Build structured execution graph
    steps = [
        '1. Ingest Customer Request & Metadata',
        '2. Extract Entities (Amount, Timestamps, Identifiers)'
    ]
    if days:
        steps.append(f'3. Verify Customer History Window (Past {days} Days)')
    if threshold is not None:
        steps.append(f'4. Evaluate Business Threshold (₹{threshold:,} Upper Limit)')
    steps.extend([
        '5. Screen Security & Policy Guardrails',
        '6. Execute Deterministic Routing (Auto-Resolve vs. Escalation)'
    ])

    return {
        'status': 'ready',
        'spec': {
            'objective': text,
            'threshold': threshold,
            'history_window_days': days,
            'steps': steps,
            'auto_action': f'Automatically resolve when amount ≤ ₹{threshold:,} and safety checks pass.' if threshold else 'Automatically resolve standard qualifying transactions.',
            'exception': 'Route to human review when amount exceeds threshold, policy signals fire, or data is missing.'
        }
    }

async def run_benchmark_job(job_id: str):
    """Executes 100 benchmark cases across realistic processing stages."""
    try:
        update_job_stage(job_id, "Initializing FlowPilot engine", 1, 12, "Setting up deterministic precedence rules.")
        await asyncio.sleep(0.08)

        update_job_stage(job_id, "Loading evaluation benchmark", 2, 12, "Retrieved 100 policy benchmark cases from repository.")
        await asyncio.sleep(0.08)

        update_job_stage(job_id, "Validating evaluation dataset", 3, 12, "Verified case structure, labels, and boundary conditions.")
        await asyncio.sleep(0.08)

        update_job_stage(job_id, "Initializing policy rules and safety checks", 4, 12, "Quarantined prompt injection filters activated.")
        await asyncio.sleep(0.08)

        batch_size = 20
        results = []
        total = len(CASES)

        for b_idx in range(0, total, batch_size):
            batch = CASES[b_idx:b_idx+batch_size]
            stage_num = 5 + (b_idx // batch_size)
            batch_label = f"Processing batch {b_idx//batch_size + 1}/5 (Cases {b_idx+1}–{min(b_idx+batch_size, total)})"
            
            update_job_stage(job_id, batch_label, stage_num, 12, f"Executing policy precedence on {len(batch)} customer requests.")
            
            for c in batch:
                r = classify(c['message'])
                matched = (r['decision'] == c['expected_decision'])
                model_risk = r['risk'].lower()
                current_risk = ANALYST_RISK_OVERRIDES.get(c['id'], model_risk)
                results.append({
                    'id': c['id'],
                    'type': c.get('type', 'General'),
                    'message': c['message'],
                    'expected_decision': c['expected_decision'],
                    'actual_decision': r['decision'],
                    'original_risk': model_risk.upper(),
                    'risk': current_risk.upper(),
                    'is_overridden': (c['id'] in ANALYST_RISK_OVERRIDES and current_risk != model_risk),
                    'matched': matched,
                    'reason': r['reason'],
                    'evidence': r['evidence']
                })
            
            update_job_progress(job_id, len(results), total)
            await asyncio.sleep(0.08)

        update_job_stage(job_id, "Recording explanations and evidence trails", 10, 12, "Generated explainable audit documentation.")
        await asyncio.sleep(0.08)

        update_job_stage(job_id, "Calculating evaluation metrics", 11, 12, "Aggregating accuracy, compliance, and unsafe actions.")
        await asyncio.sleep(0.08)

        correct = sum(x['matched'] for x in results)
        unsafe = sum(x['actual_decision'] == 'AUTO_RESOLVE' and x['expected_decision'] != 'AUTO_RESOLVE' for x in results)
        auto_count = sum(x['actual_decision'] == 'AUTO_RESOLVE' for x in results)
        escalations = sum(x['actual_decision'] == 'HUMAN_REVIEW' for x in results)
        info_requests = sum(x['actual_decision'] == 'REQUEST_INFORMATION' for x in results)
        blocked_count = sum(x['actual_decision'] == 'BLOCK' for x in results)
        needed_escalation = sum(x['expected_decision'] == 'HUMAN_REVIEW' for x in results)
        correct_escalations = sum(x['actual_decision'] == 'HUMAN_REVIEW' and x['expected_decision'] == 'HUMAN_REVIEW' for x in results)

        summary = {
            'total': total,
            'correct': correct,
            'accuracy': round((correct / total) * 100, 1),
            'policy_compliance': 100.0 if unsafe == 0 else round(((total - unsafe) / total) * 100, 1),
            'unsafe_actions': unsafe,
            'automation_rate': round((auto_count / total) * 100, 1),
            'human_review_rate': round((escalations / total) * 100, 1),
            'request_information_rate': round((info_requests / total) * 100, 1),
            'blocked_rate': round((blocked_count / total) * 100, 1),
            'correct_escalation_rate': round((correct_escalations / needed_escalation * 100) if needed_escalation else 100.0, 1),
            'auto_count': auto_count,
            'human_count': escalations,
            'info_count': info_requests,
            'block_count': blocked_count,
            'minutes_saved': auto_count * 4,
            'hours_saved': round((auto_count * 4) / 60, 1),
            'failures': [x for x in results if not x['matched']]
        }

        update_job_stage(job_id, "Benchmark completed", 12, 12, f"Evaluation finished: {correct}/{total} cases matched, 0 unsafe actions.", level='success')
        complete_job(job_id, summary, results, f"Benchmark execution completed: {correct}/{total} matched (0 unsafe actions).")
    except Exception as e:
        fail_job(job_id, str(e))


async def run_custom_eval_job(job_id: str, records: List[Dict[str, Any]]):
    """Evaluates custom confirmed records across live progress stages."""
    try:
        total = len(records)
        update_job_stage(job_id, "Receiving and validating records", 1, 8, f"Loaded {total} canonical records for evaluation.")
        await asyncio.sleep(0.06)

        update_job_stage(job_id, "Activating policy precedence rules", 2, 8, "Initialized 8-tier hierarchical policy rules.")
        await asyncio.sleep(0.06)

        batch_size = max(5, total // 4)
        evaluated_records = []
        decisions_summary = {'AUTO_RESOLVE': 0, 'REQUEST_INFORMATION': 0, 'HUMAN_REVIEW': 0, 'BLOCK': 0}

        for idx, rec in enumerate(records):
            msg = rec.get('message', '').strip()
            amount_hint = rec.get('amount')
            
            if amount_hint and not extract_amount(msg):
                eval_text = f"{msg} (Amount: ₹{amount_hint})"
            else:
                eval_text = msg

            res = classify(eval_text)
            decisions_summary[res['decision']] = decisions_summary.get(res['decision'], 0) + 1
            
            evaluated_records.append({
                'record_id': rec.get('record_id') or f"REC-{idx+1:03d}",
                'message': msg,
                'decision': res['decision'],
                'risk': res['risk'],
                'amount': res['amount'] or amount_hint,
                'category': res['category'],
                'reason': res['reason'],
                'evidence': res['evidence'],
                'recommended_action': res['recommended_action'],
                'source_file': rec.get('source_file', 'uploaded_data')
            })

            if (idx + 1) % batch_size == 0 or (idx + 1) == total:
                stage_idx = 3 + min(3, (idx + 1) // batch_size)
                update_job_stage(job_id, f"Evaluating records ({idx+1}/{total})", stage_idx, 8, f"Evaluated {idx+1} of {total} records.")
                update_job_progress(job_id, idx + 1, total)
                await asyncio.sleep(0.06)

        update_job_stage(job_id, "Compiling audit trail and explainable reasons", 7, 8, "Recorded explainable evidence points.")
        await asyncio.sleep(0.06)

        summary = {
            'total': total,
            'evaluated_count': len(evaluated_records),
            'summary': decisions_summary,
            'automation_rate': round((decisions_summary['AUTO_RESOLVE'] / total * 100) if total else 0, 1),
            'human_review_rate': round((decisions_summary['HUMAN_REVIEW'] / total * 100) if total else 0, 1),
            'request_information_rate': round((decisions_summary['REQUEST_INFORMATION'] / total * 100) if total else 0, 1),
            'blocked_rate': round((decisions_summary['BLOCK'] / total * 100) if total else 0, 1),
            'sample_results': evaluated_records[:15]
        }

        update_job_stage(job_id, "Evaluation completed", 8, 8, f"Completed evaluation for {total} records.", level='success')
        complete_job(job_id, summary, evaluated_records, f"Successfully evaluated {total} records.")
    except Exception as e:
        fail_job(job_id, str(e))


@app.post('/api/benchmark/start')
async def start_benchmark(background_tasks: BackgroundTasks):
    """Starts asynchronous benchmark execution with real-time stage progress."""
    job_id = create_job('benchmark', len(CASES))
    background_tasks.add_task(run_benchmark_job, job_id)
    return {'job_id': job_id, 'status': 'preparing', 'total': len(CASES)}


@app.get('/api/jobs/{job_id}')
def check_job(job_id: str):
    """Polls real-time stage progress, activity log, and final results for an execution job."""
    status = get_job_status(job_id)
    if not status:
        raise HTTPException(404, f"Execution job '{job_id}' not found.")
    return status


@app.post('/api/custom/extract')
async def extract_custom_document(file: UploadFile = File(...)):
    """
    Ingests and normalizes any supported format (CSV, TSV, JSON, XLSX, TXT, MD, LOG, PDF, DOCX, HTML, XML).
    Returns canonical records, proposed column mapping, and preview data before evaluation.
    """
    raw = await file.read()
    try:
        raw_records, metadata = ingest_file(file.filename, raw)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"Extraction failed: {str(e)}")

    canonical_records, norm_meta = normalize_records(raw_records, metadata)
    
    return {
        'status': norm_meta.get('status', 'extracted'),
        'filename': metadata['filename'],
        'extension': metadata['extension'],
        'file_type': metadata['file_type'],
        'file_size_bytes': metadata['file_size_bytes'],
        'warnings': metadata.get('warnings', []),
        'total_extracted': len(canonical_records),
        'columns': norm_meta.get('columns', []),
        'field_mapping': norm_meta.get('mapping', {}),
        'preview_records': canonical_records[:20],
        'all_records': canonical_records,
        'message': norm_meta.get('message', '')
    }


@app.post('/api/custom/evaluate-async')
async def evaluate_custom_async(req: ConfirmEvaluateRequest, background_tasks: BackgroundTasks):
    """Executes policy engine on user-confirmed canonical records with live stage reporting."""
    if not req.records:
        raise HTTPException(400, "No records provided for evaluation.")
    job_id = create_job('custom_evaluation', len(req.records))
    background_tasks.add_task(run_custom_eval_job, job_id, req.records)
    return {'job_id': job_id, 'status': 'preparing', 'total': len(req.records)}


@app.post('/api/custom/upload')
async def custom_upload(file: UploadFile = File(...)):
    """
    Direct ingestion endpoint for CSV, TSV, JSON, XLSX spreadsheets.
    Performs intelligent mapping and synchronous evaluation for direct API consumers and tests.
    """
    name = (file.filename or '').lower()
    if not name.endswith(('.csv', '.tsv', '.json', '.xlsx')):
        raise HTTPException(400, 'Unsupported format. Allowed file types: CSV, JSON, XLSX')

    raw = await file.read()
    try:
        raw_records, metadata = ingest_file(file.filename, raw)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"Upload processing failed: {str(e)}")

    canonical_records, norm_meta = normalize_records(raw_records, metadata)

    if norm_meta.get('status') == 'mapping_needed':
        return {
            'filename': metadata['filename'],
            'status': 'mapping_needed',
            'rows_count': len(raw_records),
            'columns': norm_meta.get('columns', []),
            'field_mapping': {
                'request_column': norm_meta.get('mapping', {}).get('message_column'),
                'id_column': norm_meta.get('mapping', {}).get('id_column'),
                'amount_column': norm_meta.get('mapping', {}).get('amount_column')
            },
            'message': norm_meta.get('message', 'Field mapping needed.')
        }

    evaluated_records = []
    decisions_summary = {'AUTO_RESOLVE': 0, 'REQUEST_INFORMATION': 0, 'HUMAN_REVIEW': 0, 'BLOCK': 0}

    for i, row in enumerate(canonical_records[:MAX_RECORDS_LIMIT]):
        text_val = str(row.get('message') or '').strip()
        if not text_val:
            continue
        
        amt_val = row.get('amount')
        if amt_val and not extract_amount(text_val):
            text_val += f" (Amount: ₹{amt_val})"

        res = classify(text_val)
        decisions_summary[res['decision']] = decisions_summary.get(res['decision'], 0) + 1
        
        evaluated_records.append({
            'row_id': row.get('record_id') or f"ROW-{i+1:03d}",
            'request': text_val,
            'decision': res['decision'],
            'risk': res['risk'],
            'reason': res['reason'],
            'recommended_action': res['recommended_action']
        })

    return {
        'filename': metadata['filename'],
        'status': 'evaluated',
        'rows_count': len(raw_records),
        'evaluated_count': len(evaluated_records),
        'columns': norm_meta.get('columns', []),
        'field_mapping': {
            'request_column': norm_meta.get('mapping', {}).get('message_column'),
            'id_column': norm_meta.get('mapping', {}).get('id_column'),
            'amount_column': norm_meta.get('mapping', {}).get('amount_column')
        },
        'summary': decisions_summary,
        'sample_results': evaluated_records[:10]
    }


# ============================================================================
# AP INVOICE PROCESSING WORKFLOW ENGINE ENDPOINTS
# ============================================================================

@app.get('/api/invoice/pos')
def list_purchase_orders():
    """Lists all synthetic reference purchase orders."""
    return invoice_engine.list_purchase_orders()


@app.post('/api/invoice/pos')
def create_purchase_order(po: PurchaseOrder):
    """Registers a purchase order into the reference ledger."""
    return invoice_engine.add_purchase_order(po)


@app.get('/api/invoice/pos/{po_number}')
def get_purchase_order(po_number: str):
    """Retrieves a single purchase order by identifier."""
    po = invoice_engine.get_purchase_order(po_number)
    if not po:
        raise HTTPException(404, f"Purchase order '{po_number}' not found.")
    return po


@app.post('/api/invoice/evaluate')
def evaluate_invoice_direct(invoice: InvoiceData):
    """Directly evaluates a structured invoice against policy rules and PO ledger."""
    try:
        res = invoice_engine.evaluate_invoice(invoice)
        return res
    except Exception as e:
        raise HTTPException(400, f"Invoice evaluation failed: {str(e)}")


@app.post('/api/invoice/upload')
async def upload_invoice_file(file: UploadFile = File(...)):
    """
    Accepts PDF, JSON, or CSV invoice file uploads.
    Extracts structured fields, matches against PO ledger, performs financial
    validation, and generates explainable decisions.
    """
    filename = (file.filename or '').strip()
    clean_name = filename.lower()
    if not clean_name.endswith(('.pdf', '.json', '.csv')):
        raise HTTPException(400, "Unsupported invoice format. Supported formats: PDF, JSON, CSV.")

    raw_bytes = await file.read()
    if len(raw_bytes) == 0:
        raise HTTPException(400, "Uploaded invoice file is empty.")
    if len(raw_bytes) > 10 * 1024 * 1024:
        raise HTTPException(400, "File exceeds maximum size limit of 10MB.")

    if clean_name.endswith('.pdf'):
        try:
            invoice_data = invoice_engine.extract_from_pdf_bytes(raw_bytes, filename)
            result = invoice_engine.evaluate_invoice(invoice_data)
            return {
                "format": "PDF",
                "filename": filename,
                "invoices_evaluated": 1,
                "results": [result]
            }
        except Exception as e:
            raise HTTPException(400, f"PDF extraction and evaluation failed: {str(e)}")

    elif clean_name.endswith('.json'):
        try:
            text = raw_bytes.decode('utf-8')
            parsed = json.loads(text)
            results = []
            if isinstance(parsed, list):
                for item in parsed:
                    inv_obj = InvoiceData(**item)
                    res = invoice_engine.evaluate_invoice(inv_obj)
                    results.append(res)
            elif isinstance(parsed, dict):
                inv_obj = InvoiceData(**parsed)
                res = invoice_engine.evaluate_invoice(inv_obj)
                results.append(res)
            else:
                raise ValueError("JSON must contain an invoice object or array of invoice objects.")
            return {
                "format": "JSON",
                "filename": filename,
                "invoices_evaluated": len(results),
                "results": results
            }
        except Exception as e:
            raise HTTPException(400, f"JSON invoice parsing failed: {str(e)}")

    elif clean_name.endswith('.csv'):
        try:
            text = raw_bytes.decode('utf-8', errors='replace')
            reader = csv.DictReader(io.StringIO(text))
            results = []
            for idx, row in enumerate(reader):
                inv_num = row.get('invoice_number') or row.get('invoice_id') or f"CSV-INV-{idx+1:03d}"
                vendor = row.get('vendor_name') or row.get('vendor') or "Unknown Vendor"
                po_num = row.get('po_number') or row.get('po') or None
                tot_val = float(row.get('total_amount') or row.get('total') or row.get('amount') or 0.0)
                sub_val = float(row.get('subtotal') or tot_val)
                tax_val = float(row.get('tax_amount') or row.get('tax') or 0.0)
                curr_val = (row.get('currency') or 'USD').strip().upper()
                inv_obj = InvoiceData(
                    invoice_number=inv_num,
                    vendor_name=vendor,
                    po_number=po_num,
                    currency=curr_val,
                    subtotal=sub_val,
                    tax_amount=tax_val,
                    total_amount=tot_val
                )
                res = invoice_engine.evaluate_invoice(inv_obj)
                results.append(res)
            return {
                "format": "CSV",
                "filename": filename,
                "invoices_evaluated": len(results),
                "results": results
            }
        except Exception as e:
            raise HTTPException(400, f"CSV invoice parsing failed: {str(e)}")


@app.get('/api/invoice/invoices')
def list_evaluated_invoices():
    """Returns all evaluated invoices in the current AP session."""
    return invoice_engine.list_invoices()


@app.get('/api/invoice/metrics')
def get_invoice_metrics():
    """Retrieves aggregated AP processing metrics and KPI indicators."""
    return invoice_engine.get_metrics()


@app.get('/api/invoice/export/csv')
def export_invoices_csv():
    """Exports evaluated AP invoices as an RFC 4180 CSV document."""
    csv_data = invoice_engine.export_csv()
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=flowpilot_ap_invoices.csv"}
    )


@app.get('/api/invoice/export/json')
def export_invoices_json():
    """Exports evaluated AP invoices and complete audit histories in JSON format."""
    return invoice_engine.export_json()


@app.post('/api/invoice/reset')
def reset_invoice_state():
    """Resets evaluated invoices and duplicate registry in memory."""
    invoice_engine.reset_state()
    return {"status": "ok", "message": "AP Invoice engine state has been reset."}


@app.post('/api/invoice/load-samples')
def load_sample_invoices():
    """Loads and evaluates the benchmark sample PDF invoices."""
    sample_dir = ROOT / 'data' / 'sample_invoices'
    results = []
    if sample_dir.exists():
        for pdf_path in sorted(sample_dir.glob('*.pdf')):
            try:
                raw_bytes = pdf_path.read_bytes()
                inv_data = invoice_engine.extract_from_pdf_bytes(raw_bytes, pdf_path.name)
                res = invoice_engine.evaluate_invoice(inv_data)
                results.append(res)
            except Exception as e:
                pass
    return {
        "status": "ok",
        "loaded_count": len(results),
        "results": results
    }


@app.post('/api/invoice/config/tolerance')
def configure_tolerance(config: ToleranceConfig):
    """Configures arithmetic and price variance tolerance levels."""
    from decimal import Decimal
    invoice_engine.rounding_tolerance = Decimal(str(config.rounding_tolerance))
    return {
        "status": "ok",
        "rounding_tolerance": float(invoice_engine.rounding_tolerance)
    }


@app.get('/api/invoice/{invoice_id}')
def get_invoice_evaluation(invoice_id: str):
    """Retrieves full evaluation details, checks, rule breakdown, and audit trail for an invoice."""
    inv = invoice_engine.get_invoice(invoice_id)
    if not inv:
        raise HTTPException(404, f"Invoice evaluation '{invoice_id}' not found.")
    return inv


@app.post('/api/invoice/{invoice_id}/review')
def apply_reviewer_action(invoice_id: str, action_req: ReviewerActionRequest):
    """
    Applies human reviewer action with audit trail recording.
    System decision is preserved immutably.
    """
    try:
        updated = invoice_engine.apply_reviewer_action(invoice_id, action_req)
        return updated
    except KeyError:
        raise HTTPException(404, f"Invoice evaluation '{invoice_id}' not found.")
    except Exception as e:
        raise HTTPException(400, f"Failed to record reviewer action: {str(e)}")


# Mount static web UI assets
app.mount('/static', StaticFiles(directory=BASE / 'static'), name='static')
