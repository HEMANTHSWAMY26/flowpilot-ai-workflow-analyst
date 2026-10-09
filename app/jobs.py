import time
import uuid
import asyncio
from datetime import datetime
from typing import Dict, Any, List, Optional

# In-memory storage for active and completed execution jobs
JOBS: Dict[str, Dict[str, Any]] = {}
MAX_STORED_JOBS = 100


def get_current_timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def create_job(job_type: str, total_records: int) -> str:
    """Initializes a new execution job."""
    # Clean up old jobs if exceeding threshold
    if len(JOBS) > MAX_STORED_JOBS:
        oldest_key = next(iter(JOBS))
        JOBS.pop(oldest_key, None)

    job_id = f"job-{uuid.uuid4().hex[:8]}"
    JOBS[job_id] = {
        'job_id': job_id,
        'job_type': job_type,
        'status': 'preparing',
        'stage': 'Initializing FlowPilot engine',
        'stage_index': 1,
        'total_stages': 12 if job_type == 'benchmark' else 8,
        'processed': 0,
        'total': total_records,
        'progress_percent': 0.0,
        'elapsed_seconds': 0.0,
        'start_time': time.time(),
        'end_time': None,
        'activity_log': [
            {
                'timestamp': get_current_timestamp(),
                'stage': 'Engine Setup',
                'message': f"Initialized {job_type} execution runner for {total_records} records.",
                'level': 'info'
            }
        ],
        'summary': None,
        'results': None,
        'error': None
    }
    return job_id


def add_job_log(job_id: str, stage: str, message: str, level: str = 'info'):
    job = JOBS.get(job_id)
    if not job:
        return
    job['activity_log'].append({
        'timestamp': get_current_timestamp(),
        'stage': stage,
        'message': message,
        'level': level
    })


def update_job_stage(
    job_id: str,
    stage_name: str,
    stage_index: int,
    total_stages: int,
    log_message: Optional[str] = None,
    level: str = 'info'
):
    job = JOBS.get(job_id)
    if not job:
        return
    job['status'] = 'running'
    job['stage'] = stage_name
    job['stage_index'] = stage_index
    job['total_stages'] = total_stages
    job['elapsed_seconds'] = round(time.time() - job['start_time'], 2)
    if log_message:
        add_job_log(job_id, stage_name, log_message, level=level)


def update_job_progress(job_id: str, processed: int, total: int, log_message: Optional[str] = None):
    job = JOBS.get(job_id)
    if not job:
        return
    job['processed'] = processed
    job['total'] = total
    job['progress_percent'] = round((processed / total * 100) if total > 0 else 0, 1)
    job['elapsed_seconds'] = round(time.time() - job['start_time'], 2)
    if log_message:
        add_job_log(job_id, job['stage'], log_message, level='info')


def complete_job(
    job_id: str,
    summary: Dict[str, Any],
    results: List[Dict[str, Any]],
    completion_message: str = "Execution completed successfully."
):
    job = JOBS.get(job_id)
    if not job:
        return
    job['status'] = 'completed'
    job['stage'] = 'Completed'
    job['stage_index'] = job['total_stages']
    job['processed'] = job['total']
    job['progress_percent'] = 100.0
    job['end_time'] = time.time()
    job['elapsed_seconds'] = round(job['end_time'] - job['start_time'], 2)
    job['summary'] = summary
    job['results'] = results
    add_job_log(job_id, 'Completed', completion_message, level='success')


def fail_job(job_id: str, error_message: str):
    job = JOBS.get(job_id)
    if not job:
        return
    job['status'] = 'failed'
    job['stage'] = 'Failed'
    job['end_time'] = time.time()
    job['elapsed_seconds'] = round(job['end_time'] - job['start_time'], 2)
    job['error'] = error_message
    add_job_log(job_id, 'Failure', f"Processing failed: {error_message}", level='error')


def get_job_status(job_id: str) -> Optional[Dict[str, Any]]:
    job = JOBS.get(job_id)
    if not job:
        return None
    
    # Calculate live elapsed time if running
    elapsed = round((time.time() - job['start_time']) if job['status'] in ('preparing', 'running') else (job['elapsed_seconds']), 2)
    
    return {
        'job_id': job['job_id'],
        'job_type': job['job_type'],
        'status': job['status'],
        'stage': job['stage'],
        'stage_index': job['stage_index'],
        'total_stages': job['total_stages'],
        'processed': job['processed'],
        'total': job['total'],
        'remaining': max(0, job['total'] - job['processed']),
        'progress_percent': job['progress_percent'],
        'elapsed_seconds': elapsed,
        'activity_log': job['activity_log'],
        'summary': job['summary'],
        'results': job['results'][:25] if job['results'] else None,
        'total_results': len(job['results']) if job['results'] else 0,
        'error': job['error']
    }
