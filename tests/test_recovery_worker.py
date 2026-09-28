"""Real QGIS snapshots survive process replacement, not only project serialization."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from smart_qgis.bridge import QgisBridge
from smart_qgis.runtime import worker_environment
from smart_qgis.tools import build_tools

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("expression,visible", [('"value" >= 2', 2), ('"value" > 99', 0)])
@pytest.mark.parametrize("fail_first", [False, True])
def test_filtered_memory_snapshot_preserves_hidden_features(tmp_path, expression, visible, fail_first):
    if not Path("/Applications/QGIS.app").exists() and not os.getenv("SMART_QGIS_PYTHON"):
        pytest.skip("Requires an installed QGIS runtime")
    executable, env = worker_environment()
    code = r'''
import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from smart_qgis.worker import Engine,protocol
from smart_qgis.reliable_ops import snapshot,restore
from qgis.core import QgsVectorLayer,QgsFeature,QgsGeometry,QgsPointXY
engine=Engine()
try:
    directory=Path(sys.argv[2]); expression=sys.argv[3]
    if sys.argv[4]=='create':
        layer=QgsVectorLayer('Point?crs=EPSG:4326&field=value:integer','filtered','memory')
        for value in (1,2,3):
            feature=QgsFeature(layer.fields());feature['value']=value
            feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(value,value)))
            assert layer.dataProvider().addFeatures([feature])[0]
        engine.project.addMapLayer(layer)
        assert layer.setSubsetString(expression)
        layer.selectByIds([f.id() for f in layer.getFeatures() if f['value']==3])
        if sys.argv[5]=='fail':
            import smart_qgis.reliable_ops as ops
            original_writer=ops.QgsVectorFileWriter
            class FailingWriter:
                SaveVectorOptions=original_writer.SaveVectorOptions
                @staticmethod
                def writeAsVectorFormatV3(*args):
                    raise OSError('Injected disk failure')
            ops.QgsVectorFileWriter=FailingWriter
            selected=layer.selectedFeatureIds()
            try:
                snapshot(engine,{'directory':str(directory/'failed-checkpoint')})
            except OSError as error:
                assert str(error)=='Injected disk failure'
            else:
                raise AssertionError('Expected write failure')
            finally:
                ops.QgsVectorFileWriter=original_writer
            assert layer.subsetString()==expression
            assert layer.selectedFeatureIds()==selected
        saved=snapshot(engine,{'directory':str(directory/'checkpoint')})
        (directory/'saved.json').write_text(json.dumps(saved))
    else:
        saved=json.loads((directory/'saved.json').read_text())
        restore(engine,saved)
        layer=engine.ordered_layers()[0]
    result={'subset':layer.subsetString(),'visible':layer.featureCount(),
            'selected_values':[f['value'] for f in layer.getSelectedFeatures()]}
    assert layer.setSubsetString('')
    result['all_values']=sorted(f['value'] for f in layer.getFeatures())
    protocol.write(json.dumps(result)+'\n')
finally:
    engine.close()
'''
    for stage in ("create", "restore"):
        result = subprocess.run(
            [executable, "-c", code, str(Path("src").resolve()), str(tmp_path), expression, stage,
             "fail" if fail_first else "normal"],
            env=env, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {
            "subset": expression, "visible": visible,
            "selected_values": [3] if visible else [], "all_values": [1, 2, 3],
        }


async def test_memory_selection_styles_layout_survive_worker_replacement(tmp_path):
    if not Path("/Applications/QGIS.app").exists() and not os.getenv("SMART_QGIS_PYTHON"):
        pytest.skip("Requires an installed QGIS runtime")
    bridge = QgisBridge(120)
    tools = {tool.name: tool for tool in build_tools(bridge)}
    try:
        features = [
            {
                "type": "Feature",
                "properties": {"value": value},
                "geometry": {"type": "Point", "coordinates": [100 + value, 30 + value]},
            }
            for value in (1, 2, 3)
        ]
        loaded = await tools["vector_data_manage"].ainvoke(
            {
                "action": "create",
                "name": "sample",
                "crs": "EPSG:4326",
                "geojson": {"type": "FeatureCollection", "features": features},
            }
        )
        layer_id = loaded["id"]
        await tools["vector_data_manage"].ainvoke(
            {
                "action": "select",
                "layer": layer_id,
                "expression": '"value" >= 2',
            }
        )
        await tools["style_vector"].ainvoke({"layer": layer_id, "color": "red", "size": 4})
        await tools["layout_manage"].ainvoke(
            {"name": "Map", "layers": [layer_id], "extent_layer": layer_id}
        )
        saved = await bridge.call("_snapshot", {"directory": str(tmp_path / "checkpoint")})
        assert saved["info"]["layers"][0]["id"] == layer_id
        assert saved["info"]["layers"][0]["provider"] == "ogr"
        await bridge.close()
        # A new bridge owns an entirely new QGIS process.
        bridge = QgisBridge(120)
        restored = await bridge.call("_restore", saved)
        assert restored["layers"][0]["id"] == layer_id
        assert restored["layouts"] == ["Map"]
        tools = {tool.name: tool for tool in build_tools(bridge)}
        output = tmp_path / "selected.geojson"
        await tools["vector_data_manage"].ainvoke(
            {
                "action": "export",
                "layer": layer_id,
                "selected_only": True,
                "path": str(output),
            }
        )
        inspected = await bridge.call("_inspect", {"source": str(output)})
        assert inspected["feature_count"] == 2
        # Inspection itself must not add a project layer.
        assert len((await tools["layer_manage"].ainvoke({}))["layers"]) == 1
        qml = tmp_path / "restored.qml"
        await tools["qml_style_manage"].ainvoke({"action": "save", "layer": layer_id, "path": str(qml)})
        assert "255,0,0,255" in qml.read_text()
        await tools["export_map"].ainvoke({"layout": "Map", "path": str(tmp_path / "restored.png")})
        assert (tmp_path / "restored.png").stat().st_size > 1000
    finally:
        await bridge.close()


async def test_unimplemented_or_external_validation_never_reports_success(tmp_path):
    if not Path("/Applications/QGIS.app").exists() and not os.getenv("SMART_QGIS_PYTHON"):
        pytest.skip("Requires an installed QGIS runtime")
    bridge = QgisBridge(120)
    try:
        result = await bridge.call(
            "_validate",
            {
                "check": {"id": "review", "kind": "external_review", "target": "map"},
                "assets": {},
            },
        )
        assert result["status"] == "unverified"
    finally:
        await bridge.close()
