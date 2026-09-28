"""Persistent GDAL display views; never rewrite source elevations or Alpha samples."""

import shutil
import uuid
from pathlib import Path
from xml.etree import ElementTree as ET

from osgeo import gdal
from qgis.core import QgsRasterLayer, QgsSingleBandPseudoColorRenderer

PENDING = "smart_qgis/pending_alpha_view"


def normalize_alpha(layer, alpha_band, temporary_directory):
    source = Path(layer.source()).resolve()
    dataset = gdal.Open(str(source))
    if dataset is None:
        raise ValueError("Cannot inspect raster Alpha")
    band = dataset.GetRasterBand(alpha_band)
    if band.DataType == gdal.GDT_Byte:
        return
    minimum, maximum = band.ComputeRasterMinMax(False)
    if minimum >= 0 and maximum <= 255:
        return
    limits = {gdal.GDT_Int16: 32767, gdal.GDT_UInt16: 65535,
              gdal.GDT_Int32: 2147483647, gdal.GDT_UInt32: 4294967295}
    limit = limits.get(band.DataType)
    if limit is None or minimum < 0 or maximum > limit:
        raise ValueError("Alpha values have no supported integer display range")
    path = Path(temporary_directory) / (uuid.uuid4().hex + ".vrt")
    view = gdal.Translate(str(path), dataset, format="VRT")
    if view is None:
        raise ValueError("Cannot create raster display view")
    view = None
    tree = ET.parse(path)
    for filename in tree.findall(".//SourceFilename"):
        if filename.get("relativeToVRT") == "1":
            filename.text = str((path.parent / filename.text).resolve())
        filename.set("relativeToVRT", "0")
    output = tree.find(f"./VRTRasterBand[@band='{alpha_band}']")
    output.set("dataType", "Byte")
    sources = [node for node in output if node.tag in {"SimpleSource", "ComplexSource"}]
    if len(sources) != 1 or sources[0].tag != "SimpleSource":
        raise ValueError("Cannot safely normalize this Alpha source mapping")
    mapped = sources[0]
    mapped.tag = "ComplexSource"
    ET.SubElement(mapped, "ScaleOffset").text = "0"
    ET.SubElement(mapped, "ScaleRatio").text = repr(255 / limit)
    tree.write(path, encoding="utf-8", xml_declaration=True)
    probe = QgsRasterLayer(str(path), layer.name(), "gdal")
    if not probe.isValid():
        raise ValueError("Raster display view is invalid")
    layer.setDataSource(str(path), layer.name(), "gdal")
    layer.setCustomProperty(PENDING, True)


def persist_views(project, directory):
    """Move temporary views into the saved project's managed dependency directory."""
    for layer in project.mapLayers().values():
        if not isinstance(layer, QgsRasterLayer) or not layer.customProperty(PENDING, False):
            continue
        destination = Path(directory) / (uuid.uuid4().hex + ".vrt")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream, Path(layer.source()).open("rb") as source:
            shutil.copyfileobj(source, stream)
        renderer = layer.renderer().clone()
        if isinstance(renderer, QgsSingleBandPseudoColorRenderer):
            renderer.setClassificationMin(layer.renderer().classificationMin())
            renderer.setClassificationMax(layer.renderer().classificationMax())
        layer.setDataSource(str(destination), layer.name(), "gdal")
        if not layer.isValid():
            raise ValueError("Persisted display view is invalid")
        layer.setRenderer(renderer)
        layer.removeCustomProperty(PENDING)
