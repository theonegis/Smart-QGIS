"""Prescribed Shaanxi workflow with six real Worker terminations after mutation.

This measures execution recovery, not autonomous planning. Keep outputs private.
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

from smart_qgis.bridge import QgisBridge, WorkerError
from smart_qgis.coordinator import EXTENSIONS, TaskCoordinator
from smart_qgis.tools import build_tools


def check(kind, target, **fields):
    return dict(id=kind + '_' + target, kind=kind, target=target, required=True,
                source='method_assumption', basis='Prescribed recovery benchmark acceptance',
                evidence=['benchmark_protocol'], **fields)


class BenchmarkCoordinator(TaskCoordinator):
    """Test-only recovery ablation; production defaults are never selectable here."""

    def __init__(self, bridge, root, policy):
        super().__init__(bridge, root)
        self.policy = policy

    async def run_with_recovery(self, step, assets, directory):
        if self.policy == 'checkpoint':
            return await super().run_with_recovery(step, assets, directory)
        retries = 2 if self.policy == 'fixed_retry' else 0
        for attempt in range(retries + 1):
            output_dir = directory / f'baseline-execution-{attempt}'
            output_dir.mkdir()
            outputs = {
                out.id: str(output_dir / (out.filename or out.id + EXTENSIONS[out.kind]))
                for out in step.outputs if out.kind != 'layout' and out.binding != 'layer'
            }
            resolved = self.resolve(step.arguments, assets, outputs)
            try:
                return await self.bridge.call(step.operation, resolved), outputs
            except WorkerError:
                if attempt == retries:
                    raise
                self.store.event('BASELINE_FIXED_RETRY', {'count': attempt + 1})
                # A broken worker must be replaced, but this baseline has no
                # checkpoint restoration or semantic argument repair.
                if self.bridge.broken:
                    await self.fresh_worker()


async def run(data, output, policy='checkpoint', fault_stage='all'):
    output.mkdir(parents=True, exist_ok=False)
    coordinator = BenchmarkCoordinator(QgisBridge(900), output / 'state', policy)
    tools = {tool.name: tool for tool in build_tools(coordinator)}
    journal = {'protocol': 'post-operation Worker termination before validation/commit',
               'recovery_policy': policy, 'fault_stage': fault_stage,
               'shared_controls': 'Same workflow, contracts, validation and persistence; only execution recovery differs',
               'autonomous_planning': False, 'faults': [], 'steps': []}
    run_started = time.monotonic()

    def record():
        (output / 'report.json').write_text(json.dumps(journal, ensure_ascii=False, indent=2))

    async def step(name, operation, arguments, outputs=(), inputs=(), fault=False, resampling=False):
        fault = fault and fault_stage in ('all', name)
        status = coordinator.compact_status()
        status = await tools['step_contract_submit'].ainvoke({
            'task_id': status['task_id'], 'continuation_token': status['continuation_token'],
            'step_id': name, 'contract': {'operation': operation, 'arguments': arguments,
                'outputs': list(outputs), 'inputs': list(inputs), 'resampling': resampling,
                'reason': 'Prescribed fault-recovery benchmark'},
        })
        original_bridge, fired = coordinator.bridge, False
        original_call = original_bridge.call
        fault_record = None

        async def terminate_after_result(op, args):
            nonlocal fired, fault_record
            result = await original_call(op, args)
            if op == operation and not fired:
                fired = True
                # Real process-group termination; successful response is discarded
                # before the coordinator can validate, snapshot or commit it.
                fault_record = {'step': name, 'operation': op, 'discarded_result': result,
                                'resolved_arguments': args, 'worker_pid': original_bridge.process.pid}
                journal['faults'].append(fault_record)
                record()
                await original_bridge.close(abort=True)
                original_bridge.broken = True
                raise WorkerError('Injected worker loss after operation, before commit')
            return result

        if fault:
            original_bridge.call = terminate_after_result
        started = time.monotonic()
        try:
            next_call = status['next_call']
            result = await tools[next_call['tool']].ainvoke(next_call['arguments'])
        except BaseException:
            journal['steps'].append({'step': name, 'duration_seconds': time.monotonic() - started,
                                     'committed': False, 'fault_injected': fired})
            raise
        if fault and not fired:
            raise AssertionError('Fault was not injected')
        if fault_record is not None:
            fault_record['committed_result'] = result
        journal['steps'].append({'step': name, 'duration_seconds': time.monotonic() - started,
                                 'attempt_id': result['attempt_id'], 'fault_injected': fired})
        record()
        print(name, 'committed', flush=True)
        return result

    def artifact(name, kind, binding='OUTPUT'):
        return {'id': name, 'kind': kind, 'binding': binding}

    async def process(name, algorithm, parameters, inputs, fault=False, resampling=False):
        await tools['algorithms'].ainvoke({'action': 'help', 'algorithm': algorithm})
        return await step(name, 'run_processing', {'algorithm': algorithm, 'parameters': parameters,
                          'load_outputs': True}, [artifact(name, 'vector' if name == 'mask' else 'raster')],
                          inputs, fault, resampling)

    try:
        kinds = {'elevation': 'raster', 'slope': 'raster', 'project': 'project', 'png': 'image', 'pdf': 'pdf'}
        status = await tools['task_begin'].ainvoke({
            'goal': 'Prescribed Shaanxi DEM recovery benchmark: preserve clipped source elevations, derive slope on a 300 metre UTM grid, and export an elevation map.',
            'inputs': {'dem': {'path': str(data / 'DEM.tif'), 'kind': 'raster'},
                       'boundary': {'path': str(data / 'ShannXi.shp'), 'kind': 'vector'}},
            'deliverables': [dict(id=key, kind=kind, description=key) for key, kind in kinds.items()],
        })
        checks = [check('readable', key, data_kind=kind) for key, kind in kinds.items()]
        checks.extend([
            check('raster_grid', 'elevation', reference='dem', tolerance=1e-8),
            check('raster_values', 'elevation', reference='dem', absolute_tolerance=0, relative_tolerance=0, scope='sample', sample_size=10000),
            check('raster_mask', 'elevation', reference='boundary', source_raster='dem', boundary_rule='pixel_center', geometry_model='transformed_vertices', scope='sample', sample_size=10000),
            check('raster_range', 'slope', minimum=0, maximum=90, scope='full'),
            check('layout_layers', 'figure', layers=['boundary_layer', 'elevation']),
            check('legend_consistent', 'figure'),
        ])
        await tools['task_contract_submit'].ainvoke({
            'task_id': status['task_id'], 'continuation_token': status['continuation_token'],
            'contract': {'requirements': {'workflow': 'Clipped elevation, slope and complete map'},
                'checks': checks, 'coverage': {'workflow': [item['id'] for item in checks]},
                'intermediates': {'figure': 'layout', 'boundary_layer': 'vector'}},
        })
        await step('load', 'load_data', {'path': 'asset:boundary', 'name': 'Boundary'},
                   [artifact('boundary_layer', 'vector', 'layer')], ['boundary'], fault=True)
        await process('mask', 'native:reprojectlayer', {'INPUT': 'asset:boundary', 'TARGET_CRS': 'EPSG:4326', 'OUTPUT': 'output:mask'}, ['boundary'])
        await process('elevation', 'gdal:cliprasterbymasklayer', {
            'INPUT': 'asset:dem', 'MASK': 'asset:mask', 'NODATA': -9999, 'CROP_TO_CUTLINE': False,
            'KEEP_RESOLUTION': True, 'CREATION_OPTIONS': 'COMPRESS=LZW|TILED=YES', 'OUTPUT': 'output:elevation'}, ['dem', 'mask'], fault=True)
        await process('projected', 'gdal:warpreproject', {
            'INPUT': 'asset:elevation', 'TARGET_CRS': 'EPSG:32649', 'TARGET_RESOLUTION': 300,
            'RESAMPLING': 1, 'NODATA': -9999, 'OUTPUT': 'output:projected'}, ['elevation'], fault=True, resampling=True)
        await process('slope', 'gdal:slope', {'INPUT': 'asset:projected', 'BAND': 1, 'SCALE': 1,
                      'AS_PERCENT': False, 'OUTPUT': 'output:slope'}, ['projected'], fault=True)
        await step('style', 'style_raster', {'layer': 'asset:elevation', 'ramp': 'Viridis'}, inputs=['elevation'], fault=True)
        await step('outline', 'style_vector', {'layer': 'asset:boundary_layer', 'color': 'transparent', 'outline': 'black', 'width': .4}, inputs=['boundary_layer'])
        await step('layout', 'layout', {'name': 'Elevation', 'title': '陕西省海拔高度空间分布图',
                   'layers': ['asset:boundary_layer', 'asset:elevation'], 'extent_layer': 'asset:boundary_layer'},
                   [artifact('figure', 'layout', 'layout')], ['boundary_layer', 'elevation'])
        for kind in ('png', 'pdf'):
            await step(kind, 'export_map', {'layout': 'asset:figure', 'path': 'output:' + kind},
                       [artifact(kind, 'image' if kind == 'png' else 'pdf', 'path')], ['figure'], fault=kind == 'png')
        await step('save', 'project', {'action': 'save', 'path': 'output:project'},
                   [artifact('project', 'project', 'path')])
        status = coordinator.compact_status()
        finished = await tools['task_finish'].ainvoke({
            'task_id': status['task_id'], 'continuation_token': status['continuation_token'],
        })
        journal['final_status'] = finished
        journal['replays'] = coordinator.store.db.execute("SELECT count(*) FROM events WHERE kind='WORKER_REPLAY'").fetchone()[0]
        expected_replays = (6 if fault_stage == 'all' else 1) if policy == 'checkpoint' else 0
        if journal['replays'] != expected_replays:
            raise AssertionError(f'Expected {expected_replays} recovered Worker losses')
        (output / 'summary.json').write_text(json.dumps({'tasks': [finished]}, ensure_ascii=False, indent=2))
    except BaseException as exc:
        journal['failure'] = {'type': type(exc).__name__, 'message': str(exc)}
        raise
    finally:
        journal['elapsed_seconds'] = time.monotonic() - run_started
        if coordinator.store:
            journal['final_status'] = coordinator.status()
            journal['fixed_retries'] = coordinator.store.db.execute(
                "SELECT count(*) FROM events WHERE kind='BASELINE_FIXED_RETRY'").fetchone()[0]
        record()
        await coordinator.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--recovery-policy', choices=['none', 'fixed_retry', 'checkpoint'], default='checkpoint')
    parser.add_argument('--fault-stage', choices=['all', 'load', 'elevation', 'projected', 'slope', 'style', 'png'], default='all')
    args = parser.parse_args()
    asyncio.run(run(args.data.resolve(), args.output.resolve(), args.recovery_policy, args.fault_stage))
