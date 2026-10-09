import pytest
from fastapi.testclient import TestClient
import json
from pathlib import Path
import openpyxl
import io
from app.main import app, classify, CASES

client = TestClient(app)

DATA_DIR = Path(__file__).resolve().parent.parent / 'data'
FRESH_JSON_PATH = DATA_DIR / 'fresh_test_cases_50.json'
FRESH_CSV_PATH = DATA_DIR / 'fresh_test_cases_50.csv'
FRESH_XLSX_PATH = DATA_DIR / 'fresh_test_cases_50.xlsx'
BENCHMARK_JSON_PATH = DATA_DIR / 'evaluation_100.json'


# ============================================================================
# 1. FRESH DATASET SCHEMA & ISOLATION INTEGRITY TESTS
# ============================================================================

def test_fresh_dataset_file_structure_and_schema():
    """Verifies fresh_test_cases_50.json structure, size, and schema."""
    assert FRESH_JSON_PATH.exists(), "fresh_test_cases_50.json must exist in data directory"
    
    with open(FRESH_JSON_PATH, 'r', encoding='utf-8') as f:
        cases = json.load(f)
        
    assert len(cases) == 50, f"Expected exactly 50 fresh cases, got {len(cases)}"
    
    required_keys = {'id', 'message', 'type', 'category', 'risk', 'expected_decision'}
    seen_ids = set()
    for c in cases:
        assert required_keys.issubset(c.keys()), f"Case {c.get('id')} missing required fields"
        assert c['id'] not in seen_ids, f"Duplicate ID found in fresh dataset: {c['id']}"
        seen_ids.add(c['id'])
        assert c['expected_decision'] in {'AUTO_RESOLVE', 'REQUEST_INFORMATION', 'HUMAN_REVIEW', 'BLOCK'}
        assert c['risk'].lower() in {'low', 'medium', 'high'}


def test_zero_leakage_and_distinctness_from_benchmark():
    """Verifies complete independence: no ID or message overlap with evaluation_100.json."""
    with open(FRESH_JSON_PATH, 'r', encoding='utf-8') as f:
        fresh_cases = json.load(f)
        
    with open(BENCHMARK_JSON_PATH, 'r', encoding='utf-8') as f:
        benchmark_cases = json.load(f)
        
    fresh_ids = {c['id'] for c in fresh_cases}
    benchmark_ids = {c['id'] for c in benchmark_cases}
    assert len(fresh_ids.intersection(benchmark_ids)) == 0, "Found colliding IDs between fresh and benchmark datasets!"
    
    fresh_msgs = {c['message'].strip().lower() for c in fresh_cases}
    benchmark_msgs = {c['message'].strip().lower() for c in benchmark_cases}
    overlap_msgs = fresh_msgs.intersection(benchmark_msgs)
    assert len(overlap_msgs) == 0, f"Found overlapping messages between datasets: {overlap_msgs}"


# ============================================================================
# 2. UPLOAD & PARSING REGRESSION TESTS (JSON, CSV, XLSX)
# ============================================================================

def test_fresh_dataset_json_upload():
    """Tests importing and evaluating fresh_test_cases_50.json via the upload API."""
    with open(FRESH_JSON_PATH, 'rb') as f:
        response = client.post('/api/custom/upload', files={'file': ('fresh_test_cases_50.json', f, 'application/json')})
        
    assert response.status_code == 200
    data = response.json()
    assert data['status'] == 'evaluated'
    assert data['rows_count'] == 50
    assert data['evaluated_count'] == 50
    assert data['field_mapping']['request_column'] == 'message'
    assert data['summary'] == {
        'AUTO_RESOLVE': 12,
        'REQUEST_INFORMATION': 4,
        'HUMAN_REVIEW': 30,
        'BLOCK': 4
    }
    assert len(data['sample_results']) == 10


def test_fresh_dataset_csv_upload():
    """Tests importing and evaluating fresh_test_cases_50.csv via the upload API."""
    with open(FRESH_CSV_PATH, 'rb') as f:
        response = client.post('/api/custom/upload', files={'file': ('fresh_test_cases_50.csv', f, 'text/csv')})
        
    assert response.status_code == 200
    data = response.json()
    assert data['status'] == 'evaluated'
    assert data['rows_count'] == 50
    assert data['evaluated_count'] == 50
    assert data['summary'] == {
        'AUTO_RESOLVE': 12,
        'REQUEST_INFORMATION': 4,
        'HUMAN_REVIEW': 30,
        'BLOCK': 4
    }


def test_fresh_dataset_xlsx_upload():
    """Tests importing and evaluating fresh_test_cases_50.xlsx via the upload API."""
    with open(FRESH_XLSX_PATH, 'rb') as f:
        response = client.post('/api/custom/upload', files={
            'file': ('fresh_test_cases_50.xlsx', f, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        })
        
    assert response.status_code == 200
    data = response.json()
    assert data['status'] == 'evaluated'
    assert data['rows_count'] == 50
    assert data['evaluated_count'] == 50
    assert data['summary'] == {
        'AUTO_RESOLVE': 12,
        'REQUEST_INFORMATION': 4,
        'HUMAN_REVIEW': 30,
        'BLOCK': 4
    }


# ============================================================================
# 3. POLICY PRECEDENCE & DECISION ACCURACY VERIFICATION
# ============================================================================

def test_fresh_dataset_policy_precedence_and_accuracy():
    """Validates 100% policy precedence compliance and zero unsafe automated actions across all 50 cases."""
    with open(FRESH_JSON_PATH, 'r', encoding='utf-8') as f:
        cases = json.load(f)
        
    unsafe_actions = 0
    correct_decisions = 0
    
    for c in cases:
        res = classify(c['message'])
        assert res['decision'] == c['expected_decision'], (
            f"Case {c['id']} failed! Expected {c['expected_decision']}, got {res['decision']}. Msg: {c['message']}"
        )
        assert res['risk'].lower() == c['risk'].lower(), (
            f"Case {c['id']} risk mismatch! Expected {c['risk']}, got {res['risk']}."
        )
        
        if res['decision'] == c['expected_decision']:
            correct_decisions += 1
            
        if res['decision'] == 'AUTO_RESOLVE' and c['expected_decision'] != 'AUTO_RESOLVE':
            unsafe_actions += 1
            
    assert correct_decisions == 50, "All 50 cases must match expected deterministic decisions"
    assert unsafe_actions == 0, "CRITICAL: Zero unsafe automated actions allowed"


# ============================================================================
# 4. DATA ISOLATION AND SERVER STATE INTEGRITY
# ============================================================================

def test_upload_does_not_mutate_original_benchmark():
    """Verifies that running custom upload does not leak into or mutate the original 100-case dataset."""
    benchmark_cases_initial = client.get('/api/cases').json()
    assert len(benchmark_cases_initial) == 100
    
    # Perform upload of fresh dataset
    with open(FRESH_JSON_PATH, 'rb') as f:
        client.post('/api/custom/upload', files={'file': ('fresh_test_cases_50.json', f, 'application/json')})
        
    # Check baseline after upload
    benchmark_cases_after = client.get('/api/cases').json()
    assert len(benchmark_cases_after) == 100
    assert [c['id'] for c in benchmark_cases_initial] == [c['id'] for c in benchmark_cases_after]
    
    # Run benchmark evaluation
    run_res = client.post('/api/run').json()
    assert run_res['total'] == 100
    assert run_res['accuracy'] == 100.0


# ============================================================================
# 5. INPUT VALIDATION & EDGE CASES
# ============================================================================

def test_empty_files_rejected():
    """Verifies that empty files across all formats return HTTP 400."""
    for filename, mime in [('empty.csv', 'text/csv'), ('empty.json', 'application/json'), ('empty.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')]:
        r = client.post('/api/custom/upload', files={'file': (filename, b'', mime)})
        assert r.status_code == 400
        assert "empty" in r.json()['detail'].lower()


def test_malformed_json_and_invalid_types():
    """Verifies that syntax errors and non-tabular JSON return HTTP 400."""
    r_bad_syntax = client.post('/api/custom/upload', files={'file': ('bad.json', b'{invalid json', 'application/json')})
    assert r_bad_syntax.status_code == 400
    assert "malformed json" in r_bad_syntax.json()['detail'].lower()
    
    r_primitives = client.post('/api/custom/upload', files={'file': ('bad_type.json', b'[1, 2, 3]', 'application/json')})
    assert r_primitives.status_code == 400


def test_unsupported_extensions():
    """Verifies that non-supported file formats return HTTP 400."""
    for ext, mime in [('doc.pdf', 'application/pdf'), ('text.txt', 'text/plain'), ('archive.zip', 'application/zip')]:
        r = client.post('/api/custom/upload', files={'file': (ext, b'sample binary or text', mime)})
        assert r.status_code == 400
        assert "unsupported format" in r.json()['detail'].lower()


def test_missing_message_column_prompts_mapping():
    """Verifies that files lacking a recognizable message/request column return mapping_needed status."""
    csv_data = "id,status,customer_name\n1,active,Alice\n2,pending,Bob\n"
    r = client.post('/api/custom/upload', files={'file': ('unmapped.csv', csv_data.encode('utf-8'), 'text/csv')})
    assert r.status_code == 200
    data = r.json()
    assert data['status'] == 'mapping_needed'
    assert 'No customer message/request column identified' in data['message']


def test_duplicate_case_ids_and_empty_rows_handling():
    """Verifies that duplicate IDs are tolerated and blank rows are skipped safely."""
    csv_data = (
        "id,request\n"
        "ROW-1,How do I reset my password?\n"
        "ROW-1,Please refund ₹1200 for order ORD-10\n"
        "ROW-2,\n"
        "ROW-3,   \n"
        "ROW-4,Someone accessed my account\n"
    )
    r = client.post('/api/custom/upload', files={'file': ('mixed.csv', csv_data.encode('utf-8'), 'text/csv')})
    assert r.status_code == 200
    data = r.json()
    assert data['status'] == 'evaluated'
    assert data['rows_count'] == 5
    assert data['evaluated_count'] == 3
    assert data['summary']['AUTO_RESOLVE'] == 2
    assert data['summary']['HUMAN_REVIEW'] == 1
