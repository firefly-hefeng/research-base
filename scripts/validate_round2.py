"""Compare bounded seed/citation workflows and retry original acquisition gaps.

Explicit network run. Existing raw files and the round-1 reports are preserved.
Seed lookup is measured separately from broad recall and never fabricates records.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dotenv import load_dotenv
from research_base.core import ResearchBase
from research_base.storage import loads, now
from research_base.workflows import seed_matches


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--graph-requests', type=int, default=8)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / '.env.local', override=False)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output = root / '.research-base' / 'round2' / stamp
    output.mkdir(parents=True, exist_ok=True)
    plans = [json.loads(s) for s in re.findall(r'```json\n(.*?)\n```', (root/'docs/use-cases.md').read_text(encoding='utf-8'), re.S)]
    reports = []
    with ResearchBase(root/'.research-base/library') as base:
        for plan in plans:
            started = time.monotonic()
            name = plan['case_id']
            baseline = json.loads((root/'.research-base/use-cases'/name/'run-result.json').read_text(encoding='utf-8'))
            cid = base.create_collection(plan['collection_name'] + ' round 2', plan['research_task'])['collection_id']
            search = plan['searches'][0]
            frontier = base.discover_frontier(cid, **{k:v for k,v in search.items() if k!='budget'}, seeds=plan['seed_checks'],
                                              budget={'max_requests': 4 + 3*len(plan['seed_checks']), 'broad_requests':4})
            frontier = base.run_worker(frontier['job_id'])
            broad_ids = [frontier['progress']['broad_job_id']]
            following = []
            for search in plan['searches'][1:]:
                job = base.discover_papers(cid, **{**search, 'budget':{'max_requests':4}})
                job = base.run_worker(job['job_id'])
                broad_ids.append(job['job_id'])
                following.append({'job_id':job['job_id'],'state':job['state'],'requests':job['progress'].get('requests')})
            identities = []
            with base.store.connect() as db:
                for jid in broad_ids:
                    identities.extend(loads(r[0]) for r in db.execute('SELECT identity FROM discovery_hits WHERE job_id=? AND stage=\'broad\'', (jid,)))
            strict_broad = [any(seed_matches(seed, identity) for identity in identities) for seed in plan['seed_checks']]
            broad_jobs = []
            for jid in broad_ids:
                observed = base.get_job(jid)
                broad_jobs.append({'job_id': jid, 'state': observed['state'],
                                   'budget': observed['payload']['budget'],
                                   'requests': observed['progress'].get('requests', 0),
                                   'budget_overshoot': observed['progress'].get('budget_overshoot', 0)})
            seed_ids = list(dict.fromkeys(pid for s in frontier['progress']['seeds'] for pid in s.get('paper_ids',[])))
            before_graph = sum(base.get_collection(cid)['counts'].values())
            graph = None
            if seed_ids:
                graph = base.run_worker(base.expand_citations(cid, seed_ids, direction='both', budget={'max_requests':args.graph_requests})['job_id'])
            after_graph = sum(base.get_collection(cid)['counts'].values())
            # Retry the original included targets; known files are not redownloaded.
            before_access = base.get_job(baseline['acquisition']['job_id'])
            before_fulltext = sum(base.get_paper(t['paper_id'])['fulltext_available'] for t in before_access['tasks'])
            retry = base.retry_acquisition(before_access['job_id'])
            after_access = base.run_worker(before_access['job_id']) if retry['retried_task_ids'] else before_access
            after_fulltext = sum(base.get_paper(t['paper_id'])['fulltext_available'] for t in after_access['tasks'])
            report = {'case_id':name,'collection_id':cid,'checked_at':now(),
                      'baseline':{'legacy_broad_found':sum(s['broad_recalled'] for s in baseline['seed_checks']),
                                  'seed_total':len(plan['seed_checks']),'legacy_targeted_found':sum(s['found_after_targeted_search'] for s in baseline['seed_checks']),
                                  'note':'Round-1 broad checks permitted title/version matching; current strict checks use per-response identity snapshots. Do not treat them as a controlled benchmark.'},
                      'current':{'strict_broad_found':sum(strict_broad),'strict_broad_checks':strict_broad,
                                 'integrated_seed_coverage':frontier['progress']['seed_coverage'],
                                 'seed_lookup_requests':frontier['progress'].get('seed_requests',0),'frontier_job_id':frontier['job_id'],
                                 'broad_jobs':broad_jobs,'broad_requests_total':sum(j['requests'] for j in broad_jobs),
                                 'frontier_state':frontier['state'],'seed_statuses':frontier['progress']['seeds'],'following_broad_jobs':following,
                                 'citation_job_id':graph['job_id'] if graph else None,'citation_state':graph['state'] if graph else None,
                                 'citation_new_records':after_graph-before_graph,'citation_progress':graph['progress'] if graph else None},
                      'acquisition':{'original_job_id':before_access['job_id'],'before_count':before_fulltext,'after_public_retry_count':after_fulltext,
                                     'total':before_access['paper_count'],'retry':retry,'state':after_access['state'],'task_count':after_access['task_count'],'tasks':after_access['tasks']},
                      'elapsed_seconds':round(time.monotonic()-started,2)}
            (output/(name+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            reports.append(report)
            print(json.dumps({'case':name,'strict_broad_found':sum(strict_broad),'seed_coverage':report['current']['integrated_seed_coverage'],
                              'seed_lookup_requests':report['current']['seed_lookup_requests'],'citation_new_records':after_graph-before_graph,
                              'fulltext_before':before_fulltext,'fulltext_after':after_fulltext,'report_dir':str(output)},ensure_ascii=True),flush=True)
    (output/'summary.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2),encoding='utf-8')
    (root/'.research-base/round2/latest.json').write_text(json.dumps({'directory':str(output),'summary':str(output/'summary.json')},ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':
    main()
