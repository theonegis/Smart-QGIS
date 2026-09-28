"""Positive and negative scientific fixtures evaluated by the real QGIS worker."""

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from smart_qgis.bridge import QgisBridge, WorkerError
from smart_qgis.coordinator import TaskCoordinator
from smart_qgis.runtime import worker_environment
from smart_qgis.task_store import TaskError, fingerprint
from smart_qgis.tools import build_tools

pytestmark = pytest.mark.integration


@pytest.fixture
def spatial_data(tmp_path):
    if not Path("/Applications/QGIS.app").exists() and not os.getenv("SMART_QGIS_PYTHON"):
        pytest.skip("Requires QGIS")
    executable, env = worker_environment()
    code = r"""
import sys
from pathlib import Path
import numpy as np
from osgeo import gdal, ogr, osr
root=Path(sys.argv[1]); srs=osr.SpatialReference(); srs.ImportFromEPSG(3857)
source=np.arange(1,401,dtype='float32').reshape(20,20)
def write(name,data,gt=(0,1,0,20,0,-1)):
 ds=gdal.GetDriverByName('GTiff').Create(str(root/name),data.shape[1],data.shape[0],1,gdal.GDT_Float32)
 ds.SetGeoTransform(gt); ds.SetProjection(srs.ExportToWkt())
 ds.GetRasterBand(1).SetNoDataValue(-9999); ds.GetRasterBand(1).WriteArray(data); ds=None
write('source.tif',source)
for name in ('alpha', 'alpha-outside', 'alpha-missing', 'alpha-values'):
 ds=gdal.GetDriverByName('GTiff').Create(str(root/(name+'.tif')),20,20,2,gdal.GDT_Float32)
 ds.SetGeoTransform((0,1,0,20,0,-1)); ds.SetProjection(srs.ExportToWkt())
 values=source.copy(); opacity=np.zeros((20,20),dtype='float32'); opacity[:,:10]=255
 if name=='alpha-outside': opacity[5,15]=255
 if name=='alpha-missing': opacity[5,5]=0
 if name=='alpha-values': values[2,2]+=1
 ds.GetRasterBand(1).SetColorInterpretation(gdal.GCI_GrayIndex)
 ds.GetRasterBand(1).WriteArray(values)
 ds.GetRasterBand(2).SetColorInterpretation(gdal.GCI_AlphaBand)
 ds.GetRasterBand(2).WriteArray(opacity); ds=None

for name,datatype,alpha_max in [('alpha-int16',gdal.GDT_Int16,32767),('alpha-uint16',gdal.GDT_UInt16,65535)]:
 ds=gdal.GetDriverByName('GTiff').Create(str(root/(name+'.tif')),20,20,2,datatype)
 ds.SetGeoTransform((0,1,0,20,0,-1)); ds.SetProjection(srs.ExportToWkt())
 ds.GetRasterBand(1).SetColorInterpretation(gdal.GCI_GrayIndex); ds.GetRasterBand(1).WriteArray(source)
 opacity=np.zeros((20,20)); opacity[:,:10]=alpha_max
 ds.GetRasterBand(2).SetColorInterpretation(gdal.GCI_AlphaBand); ds.GetRasterBand(2).WriteArray(opacity); ds=None

geo=gdal.GetDriverByName('GTiff').CreateCopy(str(root/'geographic.tif'),gdal.Open(str(root/'source.tif')))
geo_srs=osr.SpatialReference(); geo_srs.ImportFromEPSG(4326); geo.SetProjection(geo_srs.ExportToWkt()); geo=None
correct=source.copy(); correct[:,10:]=-9999
write('correct.tif',correct)
write('truncated.tif',correct[:,:5])
bad=correct.copy(); bad[5,15]=source[5,15]; write('outside.tif',bad)
bad=correct.copy(); bad[5,5]=-9999; write('missing.tif',bad)
bad=correct.copy(); bad[2,2]+=1; write('values.tif',bad)
write('rotated.tif',source,(0,1,.2,20,.1,-1))
write('rotated-crop.tif',source[3:10,2:12],(2+.2*3,1,.2,20+.1*2-3,.1,-1))
ds=ogr.GetDriverByName('GPKG').CreateDataSource(str(root/'mask.gpkg'))
ly=ds.CreateLayer('mask',srs=srs,geom_type=ogr.wkbPolygon)
f=ogr.Feature(ly.GetLayerDefn()); f.SetGeometry(ogr.CreateGeometryFromWkt('POLYGON ((0 0, 10 0, 10 20, 0 20, 0 0))')); ly.CreateFeature(f); ds=None
ds=ogr.GetDriverByName('GPKG').CreateDataSource(str(root/'offset-mask.gpkg'))
ly=ds.CreateLayer('mask',srs=srs,geom_type=ogr.wkbPolygon)
f=ogr.Feature(ly.GetLayerDefn()); f.SetGeometry(ogr.CreateGeometryFromWkt('POLYGON ((0 0, 10.2 0, 10.2 20, 0 20, 0 0))')); ly.CreateFeature(f); ds=None
"""
    result = subprocess.run(
        [executable, "-c", code, str(tmp_path)], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    return tmp_path


async def validate(bridge, root, kind, target, reference="source.tif", **params):
    assets = {
        "target": {"path": str(root / target)},
        "reference": {"path": str(root / reference)},
        "source": {"path": str(root / "source.tif")},
    }
    return await bridge.call(
        "_validate",
        {
            "check": {
                "id": "scientific_check",
                "kind": kind,
                "target": "target",
                "reference": "reference",
                **params,
            },
            "assets": assets,
        },
    )


async def test_mask_detects_outside_and_missing_valid_pixels(spatial_data):
    bridge = QgisBridge(120)
    try:
        params = {"source_raster": "source", "boundary_rule": "pixel_center", "scope": "full"}
        good = await validate(
            bridge, spatial_data, "raster_mask", "correct.tif", "mask.gpkg", **params
        )
        assert good["status"] == "passed", good
        assert good["evidence"]["tested_pixel_bands"] == 400
        outside = await validate(
            bridge, spatial_data, "raster_mask", "outside.tif", "mask.gpkg", **params
        )
        assert outside["status"] == "failed", outside
        assert outside["evidence"]["valid_pixels_outside_mask"] == 1
        missing = await validate(
            bridge, spatial_data, "raster_mask", "missing.tif", "mask.gpkg", **params
        )
        assert missing["status"] == "failed", missing
        assert missing["evidence"]["missing_valid_pixels_inside_mask"] == 1
        truncated = await validate(
            bridge, spatial_data, "raster_mask", "truncated.tif", "mask.gpkg", **params
        )
        assert truncated["status"] == "failed", truncated
        assert truncated["evidence"]["omitted_valid_pixels_outside_output_extent"] == 100
    finally:
        await bridge.close()


async def test_values_sampling_and_rotated_grid_alignment(spatial_data):
    bridge = QgisBridge(120)
    try:
        params = {"scope": "full", "absolute_tolerance": 0, "relative_tolerance": 0}
        good = await validate(bridge, spatial_data, "raster_values", "correct.tif", **params)
        assert good["status"] == "passed", good
        assert good["evidence"]["compared_valid_pixel_bands"] == 200
        bad = await validate(bridge, spatial_data, "raster_values", "values.tif", **params)
        assert bad["status"] == "failed", bad
        assert bad["evidence"]["mismatches"] == 1
        first = await validate(
            bridge,
            spatial_data,
            "raster_values",
            "correct.tif",
            scope="sample",
            sample_size=71,
            seed=12,
            absolute_tolerance=0,
            relative_tolerance=0,
        )
        second = await validate(
            bridge,
            spatial_data,
            "raster_values",
            "correct.tif",
            scope="sample",
            sample_size=71,
            seed=12,
            absolute_tolerance=0,
            relative_tolerance=0,
        )
        assert first == second
        assert first["scope"] == "sample"
        assert first["evidence"]["tested_pixel_bands"] == 71
        rotated = await validate(
            bridge, spatial_data, "raster_grid", "rotated-crop.tif", "rotated.tif", tolerance=1e-8
        )
        assert rotated["status"] == "passed", rotated
    finally:
        await bridge.close()


async def test_vector_overlap_rejects_bbox_false_positive(spatial_data):
    bridge = QgisBridge(120)
    tools = {tool.name: tool for tool in build_tools(bridge)}
    try:
        a = await tools["vector_data_manage"].ainvoke(
            {
                "action": "create",
                "name": "triangles",
                "geojson": {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {},
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [[[0, 0], [10, 0], [0, 10], [0, 0]]],
                            },
                        }
                    ],
                },
            }
        )
        b = await tools["vector_data_manage"].ainvoke(
            {
                "action": "create",
                "name": "other",
                "geojson": {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {},
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [[[8, 8], [10, 8], [10, 10], [8, 10], [8, 8]]],
                            },
                        }
                    ],
                },
            }
        )
        result = await bridge.call(
            "_validate",
            {
                "check": {
                    "id": "overlap",
                    "kind": "spatial_overlap",
                    "target": "a",
                    "reference": "b",
                },
                "assets": {"a": {"layer_id": a["id"]}, "b": {"layer_id": b["id"]}},
            },
        )
        assert result["status"] == "failed", result
    finally:
        await bridge.close()


async def test_layout_content_rejects_wrong_text_hidden_scale_and_grid(spatial_data):
    executable, env = worker_environment()
    code = r'''
import json,sys
sys.path.insert(0,sys.argv[1])
import worker
from qgis.core import (QgsLayoutItemLabel,QgsLayoutItemLegend,QgsLayoutItemScaleBar,
                       QgsCoordinateReferenceSystem)
engine=worker.Engine()
try:
 layer=engine.load_data({'path':sys.argv[2],'kind':'vector'})
 engine.layout({'name':'Map','layers':[layer['id']],'extent_layer':layer['id'],'title':'Required title'})
 layout=engine.project.layoutManager().layoutByName('Map')
 request={'check':{'id':'content','kind':'layout_content','target':'map','texts':['Required title'],
                  'require_title':True,'require_legend':True,'require_scalebar':True,
                  'require_grid':True,'grid_crs':'EPSG:4326'},'assets':{'map':{'layout':'Map'}}}
 reports={'good':engine.dispatch('_validate',request)}
 title=next(i for i in layout.items() if isinstance(i,QgsLayoutItemLabel) and i.text()=='Required title')
 title.setText('Wrong title'); reports['title']=engine.dispatch('_validate',request);title.setText('Required title')
 scale=next(i for i in layout.items() if isinstance(i,QgsLayoutItemScaleBar))
 map_item=layout.itemById('main-map')
 legend=next(i for i in layout.items() if isinstance(i,QgsLayoutItemLegend))
 def inside(item):
  return (item.positionWithUnits().x()>=map_item.positionWithUnits().x()
          and item.positionWithUnits().y()>=map_item.positionWithUnits().y()
          and item.positionWithUnits().x()+item.sizeWithUnits().width()
              <=map_item.positionWithUnits().x()+map_item.sizeWithUnits().width()
          and item.positionWithUnits().y()+item.sizeWithUnits().height()
              <=map_item.positionWithUnits().y()+map_item.sizeWithUnits().height())
 reports['inside']={'legend':inside(legend),'scale':inside(scale)}
 reports['geometry']={'map':[map_item.positionWithUnits().x(),map_item.positionWithUnits().y(),map_item.sizeWithUnits().width(),map_item.sizeWithUnits().height()], 'scale':[scale.positionWithUnits().x(),scale.positionWithUnits().y(),scale.sizeWithUnits().width(),scale.sizeWithUnits().height()]}
 scale.setExcludeFromExports(True);reports['hidden']=engine.dispatch('_validate',request);scale.setExcludeFromExports(False)
 scale.setLinkedMap(None);reports['unlinked']=engine.dispatch('_validate',request);scale.setLinkedMap(map_item)
 grid=map_item.grids().asList()[0]
 grid.setEnabled(False);reports['disabled']=engine.dispatch('_validate',request);grid.setEnabled(True)
 grid.setCrs(QgsCoordinateReferenceSystem('EPSG:3857'));reports['crs']=engine.dispatch('_validate',request)
 grid.setCrs(QgsCoordinateReferenceSystem('EPSG:4326'))
 grid.setAnnotationEnabled(False);reports['annotations']=engine.dispatch('_validate',request)
 grid.setAnnotationEnabled(True)
 legend.setExcludeFromExports(True);reports['legend']=engine.dispatch('_validate',request)
 engine.layout({'name':'中文地图','layers':[layer['id']],'extent_layer':layer['id']})
 chinese=engine.project.layoutManager().layoutByName('中文地图')
 reports['language']=next(i for i in chinese.items() if isinstance(i,QgsLayoutItemLegend)).title()
 placement=engine.layout({'name':'Outside','layers':[layer['id']],'extent_layer':layer['id'],
                          'map_element_placement':'outside'})
 outside=engine.project.layoutManager().layoutByName('Outside')
 outer_map=outside.itemById('main-map')
 outer_legend=next(i for i in outside.items() if isinstance(i,QgsLayoutItemLegend))
 outer_scale=next(i for i in outside.items() if isinstance(i,QgsLayoutItemScaleBar))
 reports['outside_area']=placement['map_element_area']
 reports['outside_width']=placement['width_mm']
 if placement['map_element_area']=='bottom':
  reports['outside']=(outer_legend.positionWithUnits().y()>=outer_map.positionWithUnits().y()+outer_map.sizeWithUnits().height()
                       and outer_scale.positionWithUnits().y()>=outer_map.positionWithUnits().y()+outer_map.sizeWithUnits().height())
 else:
  reports['outside']=(outer_legend.positionWithUnits().x()>=outer_map.positionWithUnits().x()+outer_map.sizeWithUnits().width()
                       and outer_scale.positionWithUnits().x()>=outer_map.positionWithUnits().x()+outer_map.sizeWithUnits().width())
 engine.layout({'name':'Projected','layers':[layer['id']],'extent_layer':layer['id'],
                'grid_crs':'EPSG:3857'})
 projected={'check':{'id':'projected','kind':'layout_content','target':'projected',
                     'require_grid':True,'grid_crs':'EPSG:3857'},
            'assets':{'projected':{'layout':'Projected'}}}
 reports['projected']=engine.dispatch('_validate',projected)
 worker.protocol.write(json.dumps(reports)+'\n')
finally: engine.close()
'''
    result = await asyncio.to_thread(
        subprocess.run, [executable, "-c", code, str(Path("src/smart_qgis").resolve()),
                         str(spatial_data / "mask.gpkg")],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    reports = json.loads(result.stdout)
    geometry = reports.pop("geometry")
    assert reports.pop("inside") == {"legend": True, "scale": True}, geometry
    assert reports.pop("language") == "图例", reports
    assert reports.pop("outside"), reports
    assert reports.pop("outside_area") == "bottom", reports
    assert reports.pop("outside_width") < 210, reports
    assert reports.pop("good")["status"] == "passed", reports
    assert reports.pop("projected")["status"] == "passed", reports
    assert all(report["status"] == "failed" for report in reports.values()), reports


async def test_legend_checks_detect_custom_labels(spatial_data):
    executable, env = worker_environment()
    code = r"""
import json,sys
sys.path.insert(0,sys.argv[1])
import worker
from qgis.core import QgsLayoutItemLegend,QgsMapLayerLegendUtils
engine=worker.Engine()
try:
 layer=engine.load_data({'path':sys.argv[2],'kind':'vector'})
 engine.layout({'name':'Map','layers':[layer['id']],'extent_layer':layer['id']})
 request={'check':{'id':'legend','kind':'legend_consistent','target':'map'},'assets':{'map':{'layout':'Map'}}}
 good=engine.dispatch('_validate',request)
 legend=next(i for i in engine.project.layoutManager().layoutByName('Map').items() if isinstance(i,QgsLayoutItemLegend))
 node=legend.model().rootGroup().findLayers()[0]
 QgsMapLayerLegendUtils.setLegendNodeUserLabel(node,0,'MISLEADING LABEL')
 legend.model().refreshLayerLegend(node)
 bad=engine.dispatch('_validate',request)
 worker.protocol.write(json.dumps({'good':good,'bad':bad})+'\n')
finally: engine.close()
"""
    result = await asyncio.to_thread(
        subprocess.run,
        [
            executable,
            "-c",
            code,
            str(Path("src/smart_qgis").resolve()),
            str(spatial_data / "mask.gpkg"),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    reports = json.loads(result.stdout)
    assert reports["good"]["status"] == "passed", reports
    assert reports["bad"]["status"] == "failed", reports


async def test_layout_resolves_original_input_without_guessing_duplicate_layers(spatial_data):
    bridge = QgisBridge(120)
    try:
        path = str(spatial_data / "mask.gpkg")
        layer = await bridge.call("load_data", {"path": path, "kind": "vector"})
        await bridge.call(
            "layout", {"name": "Map", "layers": [layer["id"]], "extent_layer": layer["id"]}
        )
        request = {
            "check": {
                "id": "content",
                "kind": "layout_layers",
                "target": "map",
                "layers": ["boundary"],
            },
            "assets": {"map": {"layout": "Map"}, "boundary": {"path": path, "input": True}},
        }
        good = await bridge.call("_validate", request)
        assert good["status"] == "passed", good
        await bridge.call("load_data", {"path": path, "kind": "vector", "name": "Duplicate"})
        ambiguous = await bridge.call("_validate", request)
        assert ambiguous["status"] == "failed", ambiguous
        assert "unambiguous" in str(ambiguous["evidence"])
        request["assets"]["boundary"]["layer_id"] = layer["id"]
        explicit = await bridge.call("_validate", request)
        assert explicit["status"] == "passed", explicit
    finally:
        await bridge.close()


async def raster_task(root, inputs):
    coordinator = TaskCoordinator(QgisBridge(120), root / "tasks")
    tools = {tool.name: tool for tool in build_tools(coordinator)}
    state = await tools["task_begin"].ainvoke(
        {
            "goal": "Produce a scientifically valid raster",
            "inputs": inputs,
            "deliverables": [{"id": "result", "kind": "raster", "description": "Processed raster"}],
        }
    )
    state = await tools["task_contract_submit"].ainvoke(
        {
            "task_id": state["task_id"],
            "continuation_token": state["continuation_token"],
            "contract": {
                "requirements": {"result": "A valid raster"},
                "coverage": {"result": ["read"]},
                "checks": [
                    {
                        "id": "read",
                        "kind": "readable",
                        "target": "result",
                        "data_kind": "raster",
                        "source": "user_requirement",
                        "basis": "Raster output",
                        "evidence": ["goal"],
                    }
                ],
            },
        }
    )
    return coordinator, tools, state


async def processing_step(tools, state, algorithm, params, inputs, postconditions=()):
    arguments = {"algorithm": algorithm, "parameters": params}
    state = await tools["step_contract_submit"].ainvoke(
        {
            "task_id": state["task_id"],
            "continuation_token": state["continuation_token"],
            "step_id": "process",
            "contract": {
                "operation": "run_processing",
                "arguments": arguments,
                "inputs": inputs,
                "outputs": [{"id": "result", "kind": "raster", "binding": "OUTPUT"}],
                "postconditions": list(postconditions),
                "reason": "Requested raster analysis",
            },
        }
    )
    next_call = state["next_call"]
    return await tools[next_call["tool"]].ainvoke({
        **next_call["arguments"], "include_details": True,
    })


async def test_alpha_display_view_is_durable_and_dependency_checked(spatial_data):
    source = spatial_data / 'alpha-int16.tif'
    original = fingerprint(source)
    coordinator, tools, state = await raster_task(spatial_data, {
        'dem': {'path': str(source), 'kind': 'raster'},
    })
    task_id = state['task_id']
    try:
        for step_id, operation, arguments, inputs, outputs in [
            ('load', 'load_data', {'path': 'asset:dem', 'kind': 'raster'}, ['dem'],
             [{'id': 'result', 'kind': 'raster', 'binding': 'layer'}]),
            ('style', 'style_raster', {'layer': 'asset:result', 'ramp': 'Viridis'}, ['result'], []),
        ]:
            state = await tools['step_contract_submit'].ainvoke({
                'task_id': task_id, 'continuation_token': state['continuation_token'],
                'step_id': step_id, 'contract': {'operation': operation, 'arguments': arguments,
                    'inputs': inputs, 'outputs': outputs, 'reason': 'Render the supplied raster'},
            })
            next_call = state['next_call']
            state = await tools[next_call['tool']].ainvoke(next_call['arguments'])
        cp = coordinator.store.task()['checkpoint']
        view = Path(cp['assets']['result']['path'])
        assert view.suffix == '.vrt' and view.is_relative_to(coordinator.store.directory)
        assert fingerprint(source) == original
        recorded = next(item for item in cp['fingerprints'] if item['path'] == str(view))
        assert {str(source), str(view)} <= {item['path'] for item in recorded['files']}
        await coordinator.close()
        coordinator = TaskCoordinator(QgisBridge(120), spatial_data / 'tasks')
        resumed = await coordinator.call('task_recover', {'task_id': task_id})
        assert resumed['status'] == 'READY'
        assert coordinator.assets()['result']['path'] == str(view)
        # Lost sidecar is a recovery failure, never silently drop the display mask.
        content = view.read_bytes()
        view.unlink()
        with pytest.raises(TaskError):
            await coordinator.call('task_recover', {'task_id': task_id})
        view.write_bytes(content)
        await coordinator.call('task_recover', {'task_id': task_id})
        assert fingerprint(source) == original
    finally:
        await coordinator.close()


async def test_explicit_clip_all_touched_requirement_is_checked(spatial_data):
    coordinator, tools, state = await raster_task(spatial_data, {
        "dem": {"path": str(spatial_data / "source.tif"), "kind": "raster"},
        "mask": {"path": str(spatial_data / "offset-mask.gpkg"), "kind": "vector"},
    })
    try:
        params = {"INPUT": "asset:dem", "MASK": "asset:mask", "NODATA": -9999,
                  "KEEP_RESOLUTION": True, "CROP_TO_CUTLINE": False,
                  "EXTRA": "-wo CUTLINE_ALL_TOUCHED=TRUE", "OUTPUT": "output:result"}
        result = await processing_step(
            tools,
            state,
            "gdal:cliprasterbymasklayer",
            params,
            ["dem", "mask"],
            postconditions=[{
                "id": "user_raster_mask",
                "kind": "raster_mask",
                "target": "result",
                "reference": "mask",
                "source_raster": "dem",
                "scope": "full",
                "boundary_rule": "all_touched",
                "source": "user_requirement",
                "basis": "User explicitly requires all touched mask coverage",
                "evidence": ["goal"],
            }],
        )
        mask_reports = [r for r in result["validation"] if r["id"].endswith("raster_mask")]
        assert len(mask_reports) == 1 and mask_reports[0]["status"] == "passed", result
        assert mask_reports[0]["evidence"]["boundary_rule"] == "all_touched"
        assets = {"result": {"path": result["assets"]["result"]["path"]},
                  "mask": {"path": str(spatial_data / "offset-mask.gpkg")},
                  "dem": {"path": str(spatial_data / "source.tif")}}
        for rule, expected in (("all_touched", "passed"), ("pixel_center", "failed")):
            report = await coordinator.bridge.call("_validate", {
                "check": {"id": "full", "kind": "raster_mask", "target": "result",
                          "reference": "mask", "source_raster": "dem", "scope": "full",
                          "boundary_rule": rule}, "assets": assets,
            })
            assert report["status"] == expected, report
            if rule == "pixel_center":
                assert report["evidence"]["valid_pixels_outside_mask"] == 20
        manifest = spatial_data / "candidate.json"
        manifest.write_text(json.dumps({"status": "UNCOMMITTED", "assets": {
            "dem": assets["dem"], "boundary": assets["mask"], "elevation": assets["result"],
        }}))
        executable, env = worker_environment()
        code = """
import json,sys
sys.path.insert(0,sys.argv[1])
from validate_full_reference import validate
print(json.dumps(validate(sys.argv[2],block_size=4,geometry_model='transformed_vertices',
                          candidate=True,boundary_rule=sys.argv[3])))
"""
        for rule in ("all_touched", "pixel_center"):
            reference = await asyncio.to_thread(
                subprocess.run, [executable, "-c", code, str(Path("scripts").resolve()), str(manifest), rule],
                env=env, capture_output=True, text=True, timeout=60,
            )
            assert reference.returncode == 0, reference.stderr
            measured = json.loads(reference.stdout)
            assert measured["passed"] == (rule == "all_touched"), measured
            assert measured["counts"]["tested_source_cells"] == 400
            assert measured["counts"]["outside_valid"] == (0 if rule == "all_touched" else 20)
    finally:
        await coordinator.close()


async def test_clip_family_does_not_invent_values_mask_or_nodata_requirements(spatial_data):
    coordinator, tools, state = await raster_task(
        spatial_data,
        {
            "dem": {"path": str(spatial_data / "source.tif"), "kind": "raster"},
            "mask": {"path": str(spatial_data / "mask.gpkg"), "kind": "vector"},
        },
    )
    try:
        result = await processing_step(
            tools,
            state,
            "gdal:cliprasterbymasklayer",
            {
                "INPUT": "asset:dem",
                "MASK": "asset:mask",
                "NODATA": -9999,
                "KEEP_RESOLUTION": True,
                "CROP_TO_CUTLINE": True,
                "OUTPUT": "output:result",
            },
            ["dem", "mask"],
        )
        assert all(report["status"] == "passed" for report in result["validation"]), result
        kinds = {report["id"] for report in result["validation"]}
        assert not any(name.endswith("raster_values") for name in kinds)
        assert not any(name.endswith("raster_mask") for name in kinds)
        assert not any(name.endswith("nodata") for name in kinds)
    finally:
        await coordinator.close()


async def test_invalid_processing_parameters_are_not_worker_crashes(spatial_data):
    coordinator, tools, state = await raster_task(spatial_data, {
        "dem": {"path": str(spatial_data / "source.tif"), "kind": "raster"},
        "mask": {"path": str(spatial_data / "mask.gpkg"), "kind": "vector"},
    })
    try:
        before = coordinator.status(include_details=True)
        with pytest.raises(TaskError) as typo:
            await processing_step(tools, state, "gdal:cliprasterbymasklayer", {
                "INPUT": "asset:dem", "MASK": "asset:mask", "NO_DATA": -9999,
                "OUTPUT": "output:result",
            }, ["dem", "mask"])
        assert typo.value.payload["code"] == "INVALID_PARAMETERS"
        assert typo.value.payload["evidence"]["unknown_parameters"] == ["NO_DATA"]
        assert "NODATA" in typo.value.payload["evidence"]["allowed_parameters"]
        before["correction_budget"]["rejected_submissions"] = 1
        assert coordinator.status(include_details=True) == before
        assert coordinator.store.db.execute("SELECT count(*) FROM steps").fetchone()[0] == 0
        assert coordinator.store.db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
        with pytest.raises(WorkerError) as direct:
            await coordinator.bridge.call("run_processing", {
                "algorithm": "gdal:cliprasterbymasklayer", "parameters": {"NO_DATA": -9999},
            })
        assert direct.value.code == "INVALID_PARAMETERS"
        assert coordinator.bridge.broken is False
        with pytest.raises(TaskError) as error:
            await processing_step(tools, state, "gdal:cliprasterbymasklayer", {
                "INPUT": "asset:dem", "MASK": "asset:mask", "NODATA": -9999,
                "DATA_TYPE": "not-a-valid-enum-index", "OUTPUT": "output:result",
            }, ["dem", "mask"])
        assert error.value.payload["code"] == "INVALID_PARAMETERS"
        assert error.value.payload["phase"] == "preflight"
        assert coordinator.store.db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
        assert coordinator.store.db.execute("SELECT count(*) FROM events WHERE kind='WORKER_REPLAY'").fetchone()[0] == 0
        assert "result" not in coordinator.assets()
        assert coordinator.store.db.execute("SELECT count(*) FROM steps").fetchone()[0] == 0
        await processing_step(tools, state, "gdal:cliprasterbymasklayer", {
            "INPUT": "asset:dem", "MASK": "asset:mask", "NODATA": -9999,
            "KEEP_RESOLUTION": True, "OUTPUT": "output:result",
        }, ["dem", "mask"])
        assert coordinator.correction_failures() == 0
        assert coordinator.store.db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1
    finally:
        await coordinator.close()


async def test_terrain_family_does_not_invent_coordinate_unit_requirement(spatial_data):
    coordinator, tools, state = await raster_task(
        spatial_data,
        {
            "dem": {"path": str(spatial_data / "geographic.tif"), "kind": "raster"},
        },
    )
    try:
        result = await processing_step(
            tools,
            state,
            "gdal:slope",
            {
                "INPUT": "asset:dem",
                "BAND": 1,
                "SCALE": 1,
                "OUTPUT": "output:result",
            },
            ["dem"],
        )
        assert all(
            not report["id"].endswith("coordinate_units")
            for report in result["validation"]
        )
    finally:
        await coordinator.close()


async def test_alpha_is_validity_not_elevation(spatial_data):
    bridge = QgisBridge(120)
    try:
        mask = {"source_raster": "source", "boundary_rule": "pixel_center", "scope": "full"}
        values = {"scope": "full", "absolute_tolerance": 0, "relative_tolerance": 0}
        for name, expected in (("alpha", "passed"), ("alpha-outside", "failed"),
                               ("alpha-missing", "failed")):
            result = await validate(bridge, spatial_data, "raster_mask", name + ".tif",
                                    "mask.gpkg", **mask)
            assert result["status"] == expected, result
            assert result["evidence"]["tested_pixel_bands"] == 400
        good = await validate(bridge, spatial_data, "raster_values", "alpha.tif", **values)
        assert good["status"] == "passed", good
        assert good["evidence"]["compared_valid_pixel_bands"] == 200
        bad = await validate(bridge, spatial_data, "raster_values", "alpha-values.tif", **values)
        assert bad["status"] == "failed", bad
        assert bad["evidence"]["mismatches"] == 1
        # Only data bands contribute to elevation statistics.
        result = await validate(bridge, spatial_data, "raster_range", "alpha.tif",
                                scope="full", minimum=1, maximum=390)
        assert result["status"] == "passed", result
        assert result["evidence"]["valid_pixel_bands"] == 200
    finally:
        await bridge.close()


def test_independent_reference_accepts_alpha_and_rejects_outside(spatial_data):
    executable, env = worker_environment()
    # This checker only tests export presence; format checks belong to task validation.
    export = spatial_data / 'nonempty-export-fixture'
    export.write_bytes(b'fixture' * 300)
    assets = {
        'dem': {'path': str(spatial_data / 'source.tif')},
        'boundary': {'path': str(spatial_data / 'mask.gpkg')},
        **{key: {'path': str(export)} for key in ('project', 'png', 'pdf')},
    }
    for name, expected_success in (('alpha', True), ('alpha-outside', False)):
        assets['elevation'] = {'path': str(spatial_data / (name + '.tif'))}
        summary = spatial_data / 'summary.json'
        summary.write_text(json.dumps({'tasks': [{'status': 'COMPLETED', 'assets': assets}]}))
        result = subprocess.run(
            [executable, str(Path(__file__).resolve().parents[1] / 'scripts/validate_reliable_case.py'),
             '--summary', str(summary)], env=env, capture_output=True, text=True,
        )
        assert (result.returncode == 0) == expected_success, result.stderr
        if expected_success:
            assert json.loads(result.stdout)['counts']['inside_valid_matching'] == 200
        else:
            assert 'Valid pixel outside original boundary' in result.stderr


async def test_vrt_source_change_blocks_task_resume(spatial_data):
    from smart_qgis.task_store import fingerprint

    executable, env = worker_environment()
    vrt = spatial_data / 'source.vrt'
    code = "from osgeo import gdal; import sys; gdal.Translate(sys.argv[1],sys.argv[2],format='VRT')"
    generated = subprocess.run([executable, '-c', code, str(vrt), str(spatial_data / 'source.tif')],
                               env=env, capture_output=True, text=True)
    assert generated.returncode == 0, generated.stderr
    assert len(fingerprint(vrt)['files']) == 2
    coordinator = TaskCoordinator(QgisBridge(120), spatial_data / 'state')
    try:
        tools = {tool.name: tool for tool in build_tools(coordinator)}
        status = await tools['task_begin'].ainvoke({
            'goal': 'Inspect a local virtual raster',
            'inputs': {'dem': {'path': str(vrt), 'kind': 'raster'}},
            'deliverables': [{'id': 'map', 'description': 'Map', 'kind': 'pdf'}],
        })
        inspection = await tools['inspect_data'].ainvoke({'source': str(vrt)})
        assert inspection
        changed = subprocess.run(
            [executable, '-c', "from osgeo import gdal; import sys; d=gdal.Open(sys.argv[1],gdal.GA_Update); d.GetRasterBand(1).Fill(42); d=None",
             str(spatial_data / 'source.tif')], env=env, capture_output=True, text=True,
        )
        assert changed.returncode == 0, changed.stderr
        with pytest.raises(TaskError) as error:
            await tools['task_recover'].ainvoke({'task_id': status['task_id']})
        assert error.value.payload['code'] == 'INPUT_CHANGED'
    finally:
        await coordinator.close()


@pytest.mark.parametrize("geometry_model,boundary_rule", [("original_crs", "pixel_center"),
    ("transformed_vertices", "pixel_center"), ("transformed_vertices", "all_touched")])
@pytest.mark.parametrize("candidate", [False, 'UNCOMMITTED', 'COMMITTED_STEP'])
def test_full_independent_reference_catches_mask_values_and_omitted_extent(spatial_data, geometry_model, boundary_rule, candidate):
    executable, env = worker_environment()
    assets = {'dem': {'path': str(spatial_data / 'source.tif')},
              'boundary': {'path': str(spatial_data / 'mask.gpkg')}}
    for name, field, count in (('alpha', None, 0), ('alpha-outside', 'outside_valid', 1),
                               ('alpha-missing', 'inside_missing', 1),
                               ('alpha-values', 'value_mismatches', 1),
                               ('truncated', 'inside_missing', 100)):
        assets['elevation'] = {'path': str(spatial_data / (name + '.tif'))}
        summary, report = spatial_data / 'summary.json', spatial_data / 'full-report.json'
        summary.write_text(json.dumps({'status': candidate, 'assets': assets} if candidate else
                                      {'tasks': [{'status': 'COMPLETED', 'assets': assets}]}))
        result = subprocess.run(
            [executable, str(Path(__file__).resolve().parents[1] / 'scripts/validate_full_reference.py'),
             '--candidate' if candidate else '--summary', str(summary),
             '--report', str(report), '--geometry-model', geometry_model, '--diagnostic-limit', '1',
             '--boundary-rule', boundary_rule],
            env=env, capture_output=True, text=True,
        )
        assert result.returncode == (1 if field else 0), result.stderr
        data = json.loads(report.read_text())
        assert data['input_kind'] == ({'UNCOMMITTED': 'uncommitted_candidate',
                                      'COMMITTED_STEP': 'committed_step'}.get(candidate, 'completed_task'))
        assert data['counts']['tested_source_cells'] == 400
        assert data['discrepancy_diagnostics']['diagnostic_points_tested'] <= 1
        if name == 'truncated':
            assert data['discrepancy_diagnostics']['mask_disagreements'] == 100
            assert data['discrepancy_diagnostics']['scope'] == 'sample'
        assert data['passed'] == (field is None)
        if field:
            assert data['counts'][field] == count


async def test_cross_crs_geometry_models_distinguish_curved_edges(tmp_path):
    executable, env = worker_environment()
    code = r'''
import sys
from pathlib import Path
import numpy as np
import shapely
from pyproj import Transformer
from osgeo import gdal, ogr, osr
root=Path(sys.argv[1]); tr=Transformer.from_crs(4326,3857,always_xy=True)
vertices=[tr.transform(x,y) for x,y in [(-8,42),(-8,68),(8,68),(-8,42)]]
polygon=shapely.Polygon(vertices)
cols,rows=np.meshgrid(np.arange(100)+.5,np.arange(100)+.5)
bx,by=tr.transform(-10+cols*.2,70-rows*.3)
inside=shapely.contains_xy(polygon,bx,by)
srs=osr.SpatialReference(); srs.ImportFromEPSG(4326)
for name,mask in [('source',np.ones((100,100),dtype=bool)),('original',inside)]:
 ds=gdal.GetDriverByName('GTiff').Create(str(root/(name+'.tif')),100,100,1,gdal.GDT_Float32)
 ds.SetGeoTransform((-10,.2,0,70,0,-.3)); ds.SetProjection(srs.ExportToWkt())
 ds.GetRasterBand(1).SetNoDataValue(-9999)
 ds.GetRasterBand(1).WriteArray(np.where(mask,100,-9999).astype('float32')); ds=None
srs.ImportFromEPSG(3857)
ds=ogr.GetDriverByName('GPKG').CreateDataSource(str(root/'mask.gpkg'))
ly=ds.CreateLayer('mask',srs=srs,geom_type=ogr.wkbPolygon)
f=ogr.Feature(ly.GetLayerDefn()); f.SetGeometry(ogr.CreateGeometryFromWkb(polygon.wkb)); ly.CreateFeature(f); ds=None
'''
    generated = subprocess.run([executable, '-c', code, str(tmp_path)], env=env, capture_output=True, text=True)
    assert generated.returncode == 0, generated.stderr
    bridge = QgisBridge(120)
    try:
        params = {'source_raster': 'source', 'boundary_rule': 'pixel_center', 'scope': 'full'}
        strict = await validate(bridge, tmp_path, 'raster_mask', 'original.tif', 'mask.gpkg',
                                geometry_model='original_crs', **params)
        assert strict['status'] == 'passed', strict
        assert strict['evidence']['geometry_model'] == 'original_crs'
        assert {'shapely', 'pyproj', 'gdal'} <= strict['evidence']['geometry_dependencies'].keys()
        forward = await validate(bridge, tmp_path, 'raster_mask', 'original.tif', 'mask.gpkg',
                                 geometry_model='transformed_vertices', **params)
        assert forward['status'] == 'failed', forward
        assert (forward['evidence']['valid_pixels_outside_mask'] +
                forward['evidence']['missing_valid_pixels_inside_mask']) > 0
    finally:
        await bridge.close()


def test_replay_pixel_comparison_detects_changed_values_and_grid(spatial_data):
    executable, env = worker_environment()
    script = Path(__file__).resolve().parents[1] / 'scripts/compare_fault_replays.py'
    code = '''
import runpy, sys, json
from pathlib import Path
compare=runpy.run_path(sys.argv[1])['compare']; root=Path(sys.argv[2])
print(json.dumps([compare(str(root/'correct.tif'),str(root/name)) for name in
                 ['correct.tif','values.tif','rotated.tif']]))
'''
    result = subprocess.run([executable, '-c', code, str(script), str(spatial_data)],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    good, changed, grid = json.loads(result.stdout)
    assert good['passed'] and good['pixel_bands_checked'] == 400
    assert not changed['passed'] and changed['mismatches'] == 1
    assert not grid['passed'] and grid['reason'] == 'Affine grids differ'


@pytest.mark.parametrize("raster_name,expected_right_alpha", [("alpha.tif", 0), ("source.tif", 255),
    ("alpha-int16.tif", 0), ("alpha-uint16.tif", 0)])
async def test_raster_style_preserves_alpha_pixels_after_restore(spatial_data, raster_name, expected_right_alpha):
    bridge = QgisBridge(120)
    try:
        loaded = await bridge.call('load_data', {'path': str(spatial_data / raster_name), 'kind': 'raster'})
        await bridge.call('style_raster', {'layer': loaded['id'], 'ramp': 'Viridis',
                                          'minimum': 1, 'maximum': 400})
        checkpoint = await bridge.call('_snapshot', {'directory': str(spatial_data / 'alpha-checkpoint')})
        await bridge.close()
        bridge = QgisBridge(120)
        await bridge.call('_restore', checkpoint)
        restored = await bridge.call('_snapshot', {'directory': str(spatial_data / 'alpha-restored')})
        # Render in a separate QGIS process: test actual pixel opacity, not only
        # the renderer property or a self-reported validation result.
        code = '''
import sys,json
from qgis.core import QgsApplication,QgsProject,QgsMapSettings,QgsMapRendererParallelJob
from qgis.PyQt.QtCore import QSize
from qgis.PyQt.QtGui import QColor
app=QgsApplication([],False); app.initQgis()
project=QgsProject.instance(); results=[]; colors=[]
for path in sys.argv[1:-1]:
 assert project.read(path)
 layer=next(iter(project.mapLayers().values()))
 settings=QgsMapSettings(); settings.setLayers([layer]); settings.setDestinationCrs(layer.crs())
 settings.setExtent(layer.extent()); settings.setOutputSize(QSize(200,200))
 settings.setBackgroundColor(QColor(0,0,0,0))
 job=QgsMapRendererParallelJob(settings); job.start(); job.waitForFinished(); image=job.renderedImage()
 results.append([image.pixelColor(50,100).alpha(),image.pixelColor(150,100).alpha()])
 colors.append(image.pixelColor(50,100).getRgb())
renderer=layer.renderer().clone(); layer.setDataSource(sys.argv[-1],layer.name(),'gdal'); layer.setRenderer(renderer)
job=QgsMapRendererParallelJob(settings); job.start(); job.waitForFinished()
reference=job.renderedImage().pixelColor(50,100).getRgb()
print(json.dumps({'alpha':results,'colors':colors,'reference':reference}))
'''
        executable, env = worker_environment()
        result = subprocess.run([executable, '-c', code, checkpoint['project'], restored['project'],
                                 str(spatial_data / 'alpha.tif')],
                                env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        rendered = json.loads(result.stdout)
        assert rendered['alpha'] == [[255, expected_right_alpha]] * 2
        assert rendered['colors'] == [rendered['reference']] * 2
    finally:
        await bridge.close()


@pytest.mark.parametrize("raster_name", ['source.tif', 'alpha-int16.tif', 'alpha-uint16.tif'])
async def test_generated_raster_legend_pixels_survive_worker_replacement(spatial_data, raster_name):
    from PIL import Image

    bridge = QgisBridge(120)
    try:
        loaded = await bridge.call('load_data', {'path': str(spatial_data / raster_name), 'kind': 'raster'})
        await bridge.call('style_raster', {'layer': loaded['id'], 'ramp': 'Viridis',
                                          'minimum': 1, 'maximum': 400, 'classes': 8})
        await bridge.call('layout', {'name': 'Map', 'title': 'Recovery legend test',
                                    'layers': [loaded['id']], 'extent_layer': loaded['id']})
        before, after = spatial_data / 'before.png', spatial_data / 'after.png'
        await bridge.call('export_map', {'layout': 'Map', 'path': str(before), 'dpi': 100})
        checkpoint = await bridge.call('_snapshot', {'directory': str(spatial_data / 'checkpoint')})
        await bridge.close()
        bridge = QgisBridge(120)
        await bridge.call('_restore', checkpoint)
        await bridge.call('export_map', {'layout': 'Map', 'path': str(after), 'dpi': 100})
        with Image.open(before) as a, Image.open(after) as b:
            assert a.size == b.size
            assert a.convert('RGBA').tobytes() == b.convert('RGBA').tobytes()
    finally:
        await bridge.close()


async def test_mask_raster_uses_one_category_instead_of_numeric_ramp(spatial_data):
    bridge = QgisBridge(120)
    try:
        loaded = await bridge.call('load_data', {
            'path': str(spatial_data / 'correct.tif'), 'kind': 'raster',
        })
        styled = await bridge.call('style_raster', {
            'layer': loaded['id'], 'mode': 'mask', 'color': '#666666',
            'label': 'Priority area', 'opacity': 0.5,
        })
        assert styled['mode'] == 'mask'
        await bridge.call('layout', {
            'name': 'Map', 'title': 'Priority area', 'layers': [loaded['id']],
            'extent_layer': loaded['id'], 'map_element_placement': 'outside',
        })
        checkpoint = await bridge.call('_snapshot', {
            'directory': str(spatial_data / 'mask-checkpoint'),
        })
        executable, env = worker_environment()
        code = '''
import json,sys
from qgis.core import QgsApplication,QgsLayoutItemLegend,QgsProject,QgsRasterLayer
app=QgsApplication([],False);app.initQgis()
project=QgsProject.instance();assert project.read(sys.argv[1])
layer=next(layer for layer in project.mapLayers().values() if isinstance(layer,QgsRasterLayer))
layout=project.layoutManager().layoutByName('Map')
legend=next(item for item in layout.items() if isinstance(item,QgsLayoutItemLegend))
node=legend.model().rootGroup().findLayers()[0]
print(json.dumps({'opacity':layer.opacity(),'items':[
 str(item.data(0)) for item in legend.model().layerLegendNodes(node)]}))
'''
        result = subprocess.run([executable, '-c', code, checkpoint['project']],
                                env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        rendered = json.loads(result.stdout)
        assert rendered['opacity'] == 0.5
        assert 'Priority area' in rendered['items']
        assert not any('Band 1' in item for item in rendered['items']), rendered
    finally:
        await bridge.close()


def test_independent_horn_reference_detects_values_masks_and_chunk_edges(tmp_path):
    executable, env = worker_environment()
    script = Path(__file__).resolve().parents[1] / 'scripts/validate_slope_reference.py'
    code = '''
import runpy, sys, json
from pathlib import Path
import numpy as np
from osgeo import gdal, osr
validate=runpy.run_path(sys.argv[1])['validate']; root=Path(sys.argv[2])
srs=osr.SpatialReference(); srs.ImportFromEPSG(32649)
def write(name, data):
 ds=gdal.GetDriverByName('GTiff').Create(str(root/name),data.shape[1],data.shape[0],1,gdal.GDT_Float32)
 ds.SetGeoTransform((500000,2,0,4000000,0,-3)); ds.SetProjection(srs.ExportToWkt())
 ds.GetRasterBand(1).SetNoDataValue(-9999); ds.GetRasterBand(1).WriteArray(data); ds=None
rows,cols=np.indices((520,20)); dem=(cols*2+rows*6).astype('float32')
dem[256,10]=-9999; write('dem.tif',dem)
# Plane gradient magnitude sqrt(1^2+2^2), independent analytic oracle.
slope=np.full(dem.shape,np.degrees(np.arctan(np.sqrt(5))),dtype='float32')
slope[[0,-1],:]=-9999; slope[:,[0,-1]]=-9999; slope[255:258,9:12]=-9999
write('good.tif',slope)
wrong=slope.copy(); wrong[400,10]+=1; write('wrong.tif',wrong)
mask=slope.copy(); mask[256,10]=0; write('mask.tif',mask)
# Actual GDAL output is a separate cross-check, not the oracle's implementation.
gdal.DEMProcessing(str(root/'gdal.tif'),str(root/'dem.tif'),'slope',scale=1,computeEdges=False)
print(json.dumps([validate(root/'dem.tif',root/name) for name in ['good.tif','wrong.tif','mask.tif','gdal.tif']]))
'''
    result = subprocess.run([executable, '-c', code, str(script), str(tmp_path)],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    good, wrong, mask, gdal_result = json.loads(result.stdout)
    assert good['passed'] and good['pixels_checked'] == 10400
    assert good['expected_valid'] == 518 * 18 - 9
    assert not wrong['passed'] and wrong['value_mismatches'] == 1
    assert not mask['passed'] and mask['mask_mismatches'] == 1
    assert gdal_result['passed'], gdal_result


def test_projection_reference_rejects_relabeling_and_attribute_loss(tmp_path):
    executable, env = worker_environment()
    script = Path(__file__).resolve().parents[1] / 'scripts/validate_projection_reference.py'
    code = '''
import json, runpy, sys
from pathlib import Path
from osgeo import ogr, osr
validate=runpy.run_path(sys.argv[1])['validate']; root=Path(sys.argv[2])
source={'type':'FeatureCollection','features':[{'type':'Feature','properties':{'value':10},'geometry':{'type':'Point','coordinates':[100,30]}}]}
(root/'source.geojson').write_text(json.dumps(source))
start=osr.SpatialReference(); start.ImportFromEPSG(4326); start.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
end=osr.SpatialReference(); end.ImportFromEPSG(3857); end.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
x,y,_=osr.CoordinateTransformation(start,end).TransformPoint(100,30)
for name, coords, value in [('correct',(x,y),10),('relabel',(100,30),10),('attribute',(x,y),20)]:
 ds=ogr.GetDriverByName('GPKG').CreateDataSource(str(root/(name+'.gpkg')))
 layer=ds.CreateLayer('points',end,ogr.wkbPoint); layer.CreateField(ogr.FieldDefn('value',ogr.OFTInteger))
 f=ogr.Feature(layer.GetLayerDefn()); f.SetField('value',value)
 g=ogr.Geometry(ogr.wkbPoint); g.AddPoint_2D(*coords); f.SetGeometry(g); layer.CreateFeature(f); ds=None
print(json.dumps([validate(root/'source.geojson',root/(name+'.gpkg')) for name in ['correct','relabel','attribute']]))
'''
    result = subprocess.run([executable, '-c', code, str(script), str(tmp_path)],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    correct, relabel, attribute = json.loads(result.stdout)
    assert correct['passed']
    assert relabel['crs_matches'] and not relabel['passed']
    assert relabel['invalid_or_mismatched_features'] == 1
    assert not attribute['passed'] and attribute['missing_features'] == 1


async def test_processing_nan_nodata_can_be_requested_as_json_string(spatial_data):
    bridge = QgisBridge(120)
    output = spatial_data / 'nan-clip.tif'
    try:
        help_result = await bridge.call('algorithms', {
            'action': 'help', 'algorithm': 'gdal:cliprasterbymasklayer',
        })
        data_type = next(item for item in help_result['parameters'] if item['name'] == 'DATA_TYPE')
        float_type = next(item['value'] for item in data_type['choices'] if item['label'] == 'Float32')
        assert isinstance(float_type, int)
        assert data_type['definition']['options'][float_type] == 'Float32'
        assert data_type['value_format'] == 'Integer choice value, not its label'
        await bridge.call('run_processing', {
            'algorithm': 'gdal:cliprasterbymasklayer', 'load_outputs': False,
            'parameters': {'INPUT': str(spatial_data/'source.tif'),
                           'MASK': str(spatial_data/'mask.gpkg'), 'OUTPUT': str(output),
                           'NODATA': 'nan', 'DATA_TYPE': float_type,
                           'CROP_TO_CUTLINE': False, 'KEEP_RESOLUTION': True},
        })
        metadata = await bridge.call('_inspect', {'source': str(output), 'kind': 'raster'})
        assert metadata['nodata'] == ['nan']
        coverage = await validate(bridge, spatial_data, 'raster_mask', 'nan-clip.tif', 'mask.gpkg',
                                  source_raster='source', boundary_rule='pixel_center', scope='full')
        assert coverage['status'] == 'passed', coverage
        values = await validate(bridge, spatial_data, 'raster_values', 'nan-clip.tif', 'source.tif',
                                absolute_tolerance=0, relative_tolerance=0, scope='full')
        assert values['status'] == 'passed', values
    finally:
        await bridge.close()
