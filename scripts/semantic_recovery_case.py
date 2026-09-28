"""Seed a synthetic wrong-CRS result for host-independent semantic recovery tests.

This is a prescribed execution fixture, not autonomous contract generation.
Keep generated prompts, task state and client logs outside version control.
"""

import argparse
import asyncio
import json
from pathlib import Path

from smart_qgis.bridge import QgisBridge
from smart_qgis.coordinator import TaskCoordinator
from smart_qgis.tools import build_tools


async def seed(output, policy='sequential_controls'):
    output.mkdir(parents=True, exist_ok=False)
    source = output / 'observations.geojson'
    source.write_text(json.dumps({'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'properties': {'value': value},
         'geometry': {'type': 'Point', 'coordinates': coordinate}}
        for value, coordinate in [(10, [100, 30]), (20, [101, 31]), (30, [102, 32])]
    ]}))
    coordinator = TaskCoordinator(QgisBridge(120), output / 'state')
    tools = {tool.name: tool for tool in build_tools(coordinator)}
    goal = ('Reproject the three WGS84 observations to EPSG:3857. Preserve their locations '
            'through an actual coordinate transformation and preserve every value attribute. '
            'Deliver a readable vector file. Keep the source immutable and do not relabel its CRS.')
    try:
        status = await tools['task_begin'].ainvoke({
            'goal': goal, 'inputs': {'source': {'path': str(source), 'kind': 'vector'}},
            'deliverables': [{'id': 'projected', 'kind': 'vector', 'description': 'Reprojected observations'}],
        })
        checks = []
        for kind, extra in [('readable', {'data_kind': 'vector'}), ('crs', {'expected': 'EPSG:3857'}),
                            ('fields', {'names': ['value']}), ('geometry_valid', {})]:
            checks.append({'id': kind, 'kind': kind, 'target': 'projected',
                           'source': 'user_requirement', 'basis': goal, 'evidence': ['goal'], **extra})
        status = await tools['task_contract_submit'].ainvoke({
            'task_id': status['task_id'], 'continuation_token': status['continuation_token'],
            'contract': {'requirements': {'projection': goal}, 'checks': checks,
                              'coverage': {'projection': [c['id'] for c in checks]}},
        })

        async def execute(name, operation, arguments, outputs, inputs):
            status = coordinator.compact_status()
            status = await tools['step_contract_submit'].ainvoke({
                'task_id': status['task_id'], 'continuation_token': status['continuation_token'],
                'step_id': name, 'contract': {'operation': operation, 'arguments': arguments,
                    'outputs': outputs, 'inputs': inputs, 'reason': 'Prescribed semantic-fault fixture'},
            })
            request = status['next_call']['arguments']
            result = await tools['task_execute'].ainvoke(request)
            return request, result

        await execute('load', 'load_data', {'path': 'asset:source', 'name': 'Observations'},
                      [{'id': 'observations', 'kind': 'vector', 'binding': 'layer'}], ['source'])
        request, original_result = await execute('wrong_projection', 'run_processing', {
            'algorithm': 'native:reprojectlayer', 'parameters': {
                'INPUT': 'asset:observations', 'TARGET_CRS': 'EPSG:4326', 'OUTPUT': 'output:projected'},
            'load_outputs': True}, [{'id': 'projected', 'kind': 'vector', 'binding': 'OUTPUT'}], ['observations'])
        failure = await tools['task_validate'].ainvoke({'task_id': status['task_id']})
        if failure['passed']:
            raise AssertionError('Injected wrong CRS escaped validation')
        # Repeating the exact request cannot repair semantic content. It returns
        # the committed idempotent result without duplicating project effects.
        retries = 2 if policy in ('fixed_retry', 'sequential_controls') else 0
        for _ in range(retries):
            if await tools['task_execute'].ainvoke(request) != original_result:
                raise AssertionError('Idempotent retry changed a committed result')
        repeated = await tools['task_validate'].ainvoke({'task_id': status['task_id']}) if retries else None
        restored = None
        if policy in ('checkpoint', 'checkpoint_ai', 'sequential_controls'):
            status = await tools['task_recover'].ainvoke({'task_id': status['task_id']})
            restored = await tools['task_validate'].ainvoke({'task_id': status['task_id']})
        if any(item and item['passed'] for item in (repeated, restored)):
            raise AssertionError('Technical retry or checkpoint restore hid the semantic error')
        report = {'task_id': status['task_id'], 'state_directory': str(output / 'state'),
                  'autonomous_planning': False, 'fault': 'Wrong target CRS in a valid committed output',
                  'policy': policy, 'initial_validation': failure, 'identical_request_retries': retries,
                  'after_identical_retries': repeated, 'after_checkpoint_restore': restored,
                  'attempts_before_ai': coordinator.store.db.execute('SELECT count(*) FROM attempts').fetchone()[0],
                  'contract_version_before_ai': coordinator.store.task()['contract_version'],
                  'limitations': ['Prescribed semantic defect and prewritten contract; not autonomous planning',
                                  'Final contract alone does not prove transformed coordinates or attribute preservation']}
        (output / 'fixture.json').write_text(json.dumps(report, indent=2))
        (output / 'prompt.txt').write_text(
            'Use only Smart-QGIS MCP tools; do not use code or terminal tools.\n' + goal + '\n'
            'An existing task has a failed final acceptance check. Read its persisted contract and actual '
            'data, diagnose the failure, and propose and execute a repair without weakening requirements. '
            'Preserve valid upstream work. Only report success after task_finish for the original task.\n')
        print(json.dumps({'task_id': status['task_id'], 'initial_passed': failure['passed'],
                          'retry_passed': repeated['passed'] if repeated else None,
                          'restore_passed': restored['passed'] if restored else None}))
        return report
    finally:
        await coordinator.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--policy', choices=['none', 'fixed_retry', 'checkpoint', 'checkpoint_ai', 'sequential_controls'], default='sequential_controls')
    args = parser.parse_args()
    asyncio.run(seed(args.output.resolve(), args.policy))
