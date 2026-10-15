#!/usr/bin/env python3
"""
DSW_GridXtract — QGIS Plugin
==============================================
Version: 2.0.00 (Process Mode Selection + Dynamic Folder/Files + Logging + Track Stats)
Garmin Internal Use only....!!!!
Author : Rama Kumar Vathada
"""

import os
import secrets
import sys
import time
from datetime import datetime

from qgis.core import (
    QgsVectorLayer, QgsProject, Qgis, QgsSymbol,
    QgsCategorizedSymbolRenderer, QgsRendererCategory,
    QgsSingleSymbolRenderer, QgsApplication, QgsTask,
    QgsFeatureRequest, QgsSpatialIndex, QgsFeature,
    QgsVectorFileWriter, QgsGeometry, QgsFields, QgsField
)
from qgis.gui import QgsFileWidget
from qgis.PyQt.QtCore import Qt, QTimer, QVariant
from qgis.PyQt.QtWidgets import (
    QDialog, QPushButton, QFormLayout, 
    QMessageBox, QVBoxLayout, QHBoxLayout, QProgressBar, QLabel, QGroupBox,
    QButtonGroup, QWidget
)
from qgis.PyQt.QtGui import QIcon, QColor

# Qt5 vs Qt6 Compat
try:
    from qgis.PyQt.QtGui import QAction
except ImportError:
    from qgis.PyQt.QtWidgets import QAction

try:
    NO_BRUSH_STYLE = Qt.BrushStyle.NoBrush
    ALIGN_RIGHT = Qt.AlignmentFlag.AlignRight
    ALIGN_VCENTER = Qt.AlignmentFlag.AlignVCenter
except AttributeError:
    NO_BRUSH_STYLE = Qt.NoBrush
    ALIGN_RIGHT = Qt.AlignRight
    ALIGN_VCENTER = Qt.AlignVCenter


class AllInOneQaTask(QgsTask):
    """Pipeline: Cascades AOI -> Sonar -> Errors (or just AOI based on mode) instantly."""
    def __init__(self, description, mode, grid_path, aoi_path, sonar_path, errors_path, out_dir, date_suffix, transform_context, callback):
        super().__init__(description, QgsTask.TaskFlag.CanCancel)
        self.mode = mode
        self.grid_path = grid_path
        self.aoi_path = aoi_path
        self.sonar_path = sonar_path
        self.errors_path = errors_path
        self.out_dir = out_dir
        self.date_suffix = date_suffix
        self.transform_context = transform_context
        self.callback = callback
        
        self.error_msg = ""
        # Added new tracking keys for highest count and > 250 track tiles
        self.stats = {
            "pending": 0, "dynamic": 0, "static": 0, "err_with": 0, "err_no": 0,
            "max_track_count": 0, "err_with_250_plus": 0, "err_no_250_plus": 0
        }
        
        # Dynamically named Output paths
        self.p_pending = os.path.join(out_dir, f"DSW_Pending_tiles_{self.date_suffix}.shp")
        self.p_dynamic = os.path.join(out_dir, f"Dynamic_tiles_{self.date_suffix}.shp")
        self.p_static = os.path.join(out_dir, f"Static_tiles_{self.date_suffix}.shp")
        self.p_err_with = os.path.join(out_dir, f"Dynamic_tiles_with Errors_{self.date_suffix}.shp")
        self.p_err_no = os.path.join(out_dir, f"Dynamic_tiles_without_Errors_{self.date_suffix}.shp")

    def run(self):
        writers = []
        try:
            self.setProgress(5)
            
            grid_layer = QgsVectorLayer(self.grid_path, "grid_temp", "ogr")
            aoi_layer = QgsVectorLayer(self.aoi_path, "aoi_temp", "ogr")
            
            layers_to_check = [(grid_layer, "Grid"), (aoi_layer, "AOI")]
            
            if self.mode == "Full Process":
                sonar_layer = QgsVectorLayer(self.sonar_path, "sonar_temp", "ogr")
                errors_layer = QgsVectorLayer(self.errors_path, "errors_temp", "ogr")
                layers_to_check.extend([(sonar_layer, "Sonar"), (errors_layer, "Errors")])

            for lyr, name in layers_to_check:
                if not lyr.isValid():
                    self.error_msg = f"{name} layer is invalid or missing."
                    return False

            target_crs = grid_layer.crs()
            ctx = self.transform_context
            total_grid_features = grid_layer.featureCount()
            if total_grid_features <= 0:
                total_grid_features = 70000

            self.setProgress(10)
            
            # Prepare AOI
            aoi_req = QgsFeatureRequest().setNoAttributes().setDestinationCrs(target_crs, ctx)
            aoi_geometries = [f.geometry() for f in aoi_layer.getFeatures(aoi_req) if not f.geometry().isNull()]
            if not aoi_geometries:
                self.error_msg = "AOI layer has no valid geometries."
                return False

            merged_aoi = QgsGeometry.unaryUnion(aoi_geometries)
            if merged_aoi.isNull(): merged_aoi = QgsGeometry.collectGeometry(aoi_geometries)
            aoi_engine = QgsGeometry.createGeometryEngine(merged_aoi.constGet())
            aoi_engine.prepareGeometry()

            self.setProgress(15)

            if self.mode == "Full Process":
                # Prepare Sonar
                sonar_req = QgsFeatureRequest().setNoAttributes().setDestinationCrs(target_crs, ctx)
                sonar_index = QgsSpatialIndex()
                sonar_geoms = {}
                for f in sonar_layer.getFeatures(sonar_req):
                    geom = f.geometry()
                    if not geom.isNull():
                        sonar_index.addFeature(f)
                        sonar_geoms[f.id()] = geom

                self.setProgress(20)

                # Prepare Errors
                err_req = QgsFeatureRequest().setNoAttributes().setDestinationCrs(target_crs, ctx)
                err_index = QgsSpatialIndex()
                err_geoms = {}
                for f in errors_layer.getFeatures(err_req):
                    geom = f.geometry()
                    if not geom.isNull():
                        err_index.addFeature(f)
                        err_geoms[f.id()] = geom

            self.setProgress(25)

            save_opts = QgsVectorFileWriter.SaveVectorOptions()
            save_opts.driverName = "ESRI Shapefile"
            save_opts.fileEncoding = "UTF-8"
            save_opts.actionOnExistingFile = QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile
            
            base_fields, wkb, crs = grid_layer.fields(), grid_layer.wkbType(), grid_layer.crs()
            
            w_pending = QgsVectorFileWriter.create(self.p_pending, base_fields, wkb, crs, ctx, save_opts)
            writers.append(w_pending)

            if self.mode == "Full Process":
                dynamic_fields = QgsFields(base_fields)
                dynamic_fields.append(QgsField("Tracks_Count", QVariant.Int))

                w_static   = QgsVectorFileWriter.create(self.p_static, base_fields, wkb, crs, ctx, save_opts)
                w_dynamic  = QgsVectorFileWriter.create(self.p_dynamic, dynamic_fields, wkb, crs, ctx, save_opts)
                w_err_with = QgsVectorFileWriter.create(self.p_err_with, dynamic_fields, wkb, crs, ctx, save_opts)
                w_err_no   = QgsVectorFileWriter.create(self.p_err_no, dynamic_fields, wkb, crs, ctx, save_opts)
                
                writers.extend([w_static, w_dynamic, w_err_with, w_err_no])

            if any(w.hasError() != QgsVectorFileWriter.WriterError.NoError for w in writers):
                self.error_msg = "Failed to initialize one or more output shapefiles."
                return False

            batch_size = 5000
            buf_pending, buf_static, buf_dynamic, buf_err_with, buf_err_no = [], [], [], [], []

            def flush_buffers():
                if buf_pending: w_pending.addFeatures(buf_pending); buf_pending.clear()
                if self.mode == "Full Process":
                    if buf_static: w_static.addFeatures(buf_static); buf_static.clear()
                    if buf_dynamic: w_dynamic.addFeatures(buf_dynamic); buf_dynamic.clear()
                    if buf_err_with: w_err_with.addFeatures(buf_err_with); buf_err_with.clear()
                    if buf_err_no: w_err_no.addFeatures(buf_err_no); buf_err_no.clear()

            grid_req = QgsFeatureRequest().setFilterRect(merged_aoi.boundingBox())
            iteration = 0
            
            for feat in grid_layer.getFeatures(grid_req):
                if self.isCanceled(): break
                
                iteration += 1
                if iteration % 2500 == 0:
                    progress_ratio = iteration / total_grid_features
                    calc_prog = 25 + (progress_ratio * 73)
                    self.setProgress(min(98, int(calc_prog)))

                geom = feat.geometry()
                if geom.isNull(): continue

                bbox = geom.boundingBox()
                center_geom = QgsGeometry.fromPointXY(bbox.center())
                
                # -- 1. AOI FILTER --
                if not aoi_engine.contains(center_geom.constGet()):
                    continue
                
                buf_pending.append(feat)
                self.stats["pending"] += 1
                
                if self.mode == "pending_only":
                    if iteration % batch_size == 0:
                        flush_buffers()
                    continue

                geom_engine = None 
                
                # -- 2. SONAR FILTER --
                track_count = 0
                sonar_cids = sonar_index.intersects(bbox)
                
                if sonar_cids:
                    geom_const = geom.constGet()
                    geom_engine = QgsGeometry.createGeometryEngine(geom_const)
                    geom_engine.prepareGeometry()
                    
                    for cid in sonar_cids:
                        if geom_engine.intersects(sonar_geoms[cid].constGet()):
                            track_count += 1
                            
                if track_count > self.stats["max_track_count"]:
                    self.stats["max_track_count"] = track_count
                        
                if track_count == 0:
                    buf_static.append(feat)
                    self.stats["static"] += 1
                    
                    if iteration % batch_size == 0: flush_buffers()
                    continue 
                
                dyn_feat = QgsFeature(dynamic_fields)
                dyn_feat.setGeometry(geom)
                dyn_feat.setAttributes(feat.attributes() + [track_count]) 

                buf_dynamic.append(dyn_feat)
                self.stats["dynamic"] += 1
                
                # -- 3. ERRORS FILTER --
                error_hit = False
                err_cids = err_index.intersects(bbox)
                
                if err_cids:
                    if geom_engine is None: 
                        geom_engine = QgsGeometry.createGeometryEngine(geom.constGet())
                        geom_engine.prepareGeometry()
                        
                    for cid in err_cids:
                        if geom_engine.intersects(err_geoms[cid].constGet()):
                            error_hit = True
                            break
                        
                if error_hit:
                    buf_err_with.append(dyn_feat)
                    self.stats["err_with"] += 1
                    if track_count > 250:
                        self.stats["err_with_250_plus"] += 1
                else:
                    buf_err_no.append(dyn_feat)
                    self.stats["err_no"] += 1
                    if track_count > 250:
                        self.stats["err_no_250_plus"] += 1

                if iteration % batch_size == 0:
                    flush_buffers()

            flush_buffers()

            for w in writers:
                del w
            writers.clear()
            
            self.setProgress(100)
            return True

        except Exception as e:
            self.error_msg = str(e)
            for w in writers: del w
            return False

    def finished(self, result):
        self.callback(result, self.error_msg, self.stats, self.mode)


class DSW_GridXtractDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle("DSW GridXtract Plugin (v2.0.00)")
        self.setFixedSize(600, 305)
        
        self.setWindowOpacity(0.97) 
        
        self.setStyleSheet("""
            QDialog {
                background-color: rgba(224, 235, 235, 180);
            }
            QGroupBox {
                background-color: rgba(255, 255, 255, 60);
                border: 1px solid rgba(255, 255, 255, 200);
                border-radius: 5px;
                margin-top: 15px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 5px;
                color: #003399;
                font-weight: bold;
                left: 10px;
            }
            QLabel {
                color: #1a1a1a;
                font-size: 12px;
            }
            QPushButton {
                border: 1px solid #707070;
                border-radius: 3px;
                background-color: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
                                                  stop: 0 #f2f2f2, stop: 0.5 #ebebeb,
                                                  stop: 0.51 #dddddd, stop: 1 #cfcfcf);
                padding: 5px 15px;
                color: black;
            }
            QPushButton:hover {
                border: 1px solid #3c7fb1;
                background-color: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
                                                  stop: 0 #eaf6fd, stop: 0.5 #d9f0fc,
                                                  stop: 0.51 #bee6fd, stop: 1 #a7d9f5);
            }
            QPushButton:pressed {
                border: 1px solid #2c628b;
                background-color: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
                                                  stop: 0 #e5f4fc, stop: 0.5 #c4e5f6,
                                                  stop: 0.51 #98d1ef, stop: 1 #68b3db);
            }
            QPushButton:disabled {
                border: 1px solid #999999;
                background-color: #cccccc;
                color: #777777;
            }
            QProgressBar {
                border: 1px solid #bcbcbc;
                border-radius: 3px;
                text-align: center;
                background-color: #e6e6e6;
                color: black;
                font-weight: bold;
            }
            QProgressBar::chunk {
                background-color: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
                                                  stop: 0 #85ed73, stop: 0.5 #57d943,
                                                  stop: 0.51 #3fce2a, stop: 1 #22b10f);
                border-radius: 2px;
            }
        """)

        self.timer = QTimer()
        self.timer.timeout.connect(self._update_time_and_status)
        self.start_time = 0
        
        self.out_dir = ""
        self.date_suffix = ""
        
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout()
        
        grp_mode = QGroupBox("Select Required Extraction Mode")
        
        mode_widget = QWidget()
        mode_layout = QHBoxLayout(mode_widget)
        mode_layout.setContentsMargins(4, 2, 4, 2)
        mode_layout.setSpacing(40)

        self.btn_all = QPushButton("Full GridXtract Process")
        self.btn_pending = QPushButton("DSW Pending Tiles")

        self.btn_all.setCheckable(True)
        self.btn_pending.setCheckable(True)
        self.btn_all.setChecked(True)

        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_group.addButton(self.btn_all)
        self.mode_group.addButton(self.btn_pending)

        mode_button_style = """
            QPushButton {
                border: 1px solid transparent;
                border-radius: 3px;
                background-color: transparent;
                padding: 4px 6px;       
                min-height: 15px;
                min-width: 120px;          
            }
            QPushButton:hover {
                background-color: #eeeeee;
            }
            QPushButton:checked {
                background-color: #53c68c;
                border: 1px solid #cfd9ce;
                font-weight: bold;
            }
            QPushButton:pressed {
                background-color: #53c68c;
            }
        """
        self.btn_all.setStyleSheet(mode_button_style)
        self.btn_pending.setStyleSheet(mode_button_style)

        mode_layout.addWidget(self.btn_all)
        mode_layout.addWidget(self.btn_pending)
        
        vbox_mode = QVBoxLayout()
        vbox_mode.addWidget(mode_widget)
        grp_mode.setLayout(vbox_mode)
        layout.addWidget(grp_mode)
        
        self.btn_all.toggled.connect(self._toggle_inputs)
        
        grp_inputs = QGroupBox("Select Input Layers")
        form = QFormLayout()
        
        self._grid_file = QgsFileWidget()
        self._grid_file.setStorageMode(QgsFileWidget.StorageMode.GetFile)
        self._grid_file.setFilter("Shapefiles (*.shp)")
        
        self._aoi_file = QgsFileWidget()
        self._aoi_file.setStorageMode(QgsFileWidget.StorageMode.GetFile)
        self._aoi_file.setFilter("Vector Layers (*.shp *.kml *.geojson);;Shapefiles (*.shp)")
        
        self._sonar_file = QgsFileWidget()
        self._sonar_file.setStorageMode(QgsFileWidget.StorageMode.GetFile)
        self._sonar_file.setFilter("Vector Layers (*.shp *.kml *.geojson);;Shapefiles (*.shp)")
        
        self._errors_file = QgsFileWidget()
        self._errors_file.setStorageMode(QgsFileWidget.StorageMode.GetFile)
        self._errors_file.setFilter("Vector Layers (*.shp *.kml *.geojson);;Shapefiles (*.shp)")
        
        form.addRow("1. DSW Grid (dsw_WGS84_grid):", self._grid_file)
        form.addRow("2. Pending_Tiles_AOI (CPS):", self._aoi_file)
        
        self.lbl_sonar = QLabel("3. Sonar Tracks File (ftp_file):")
        form.addRow(self.lbl_sonar, self._sonar_file)
        
        self.lbl_errors = QLabel("4. To_Be_Errors File:")
        form.addRow(self.lbl_errors, self._errors_file)
        
        grp_inputs.setLayout(form)
        layout.addWidget(grp_inputs)

        self.run_button = QPushButton(" Run GridXtract Process")
        self.run_button.setStyleSheet("""
            QPushButton {
                font-weight: bold; font-size: 12px; padding: 4px; 
                background-color: #32b00c; color: white; border-radius: 3px;
            }
            QPushButton:disabled { background-color: #888; }
        """)
        self.run_button.clicked.connect(self._handle_processing)
        layout.addWidget(self.run_button)

        self.status_layout = QHBoxLayout()
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setFixedHeight(22)
        self.status_layout.addWidget(self.progress_bar, stretch=5)
        
        self.time_label = QLabel("")
        self.time_label.setAlignment(ALIGN_RIGHT | ALIGN_VCENTER)
        self.time_label.setStyleSheet("font-weight: bold; color: #003399;")
        self.time_label.setVisible(False)
        self.status_layout.addWidget(self.time_label, stretch=4)
        
        layout.addLayout(self.status_layout)
        self.setLayout(layout)

    def _toggle_inputs(self):
        is_all = self.btn_all.isChecked()
        self._sonar_file.setEnabled(is_all)
        self._errors_file.setEnabled(is_all)
        self.lbl_sonar.setEnabled(is_all)
        self.lbl_errors.setEnabled(is_all)

    def _update_time_and_status(self):
        elapsed = int(time.time() - self.start_time)
        mins, secs = divmod(elapsed, 60)
        prog = self.progress_bar.value()
        
        if prog < 25: status = "Loading & Indexing Geometry Filtering..."
        elif prog < 75: status = "Spatial Filtering Inprogress..."
        elif prog < 90: status = "Spatial Filtering done and Tiles Extraction Process..."
        elif prog < 100: status = "Finalizing Write..."
        else: status = "Process Finished"

        self.time_label.setText(f"{status}  |  ⏱ {mins:02d}:{secs:02d}")

    def _apply_symbology(self, layer, random_color=True):
        if not layer.isValid() or layer.featureCount() == 0: return
        fields = layer.fields()
        target_field = fields[0].name() if fields.size() > 0 else ""

        if target_field:
            unique_values = layer.uniqueValues(layer.fields().indexOf(target_field))
            if len(unique_values) > 100 or not random_color:
                symbol = QgsSymbol.defaultSymbol(layer.geometryType())
                if symbol.symbolLayerCount() > 0:
                    symbol.symbolLayer(0).setBrushStyle(NO_BRUSH_STYLE)
                    color = QColor(255, 0, 0, 255) if random_color else QColor(31, 119, 180, 255)
                    symbol.symbolLayer(0).setStrokeColor(color)
                    symbol.symbolLayer(0).setStrokeWidth(0.75)
                layer.setRenderer(QgsSingleSymbolRenderer(symbol))
            else:
                categories = []
                for value in unique_values:
                    symbol = QgsSymbol.defaultSymbol(layer.geometryType())
                    if symbol.symbolLayerCount() > 0:
                        symbol.symbolLayer(0).setBrushStyle(NO_BRUSH_STYLE)
                        symbol.symbolLayer(0).setStrokeColor(QColor(secrets.randbelow(256), secrets.randbelow(256), secrets.randbelow(256), 255))
                        symbol.symbolLayer(0).setStrokeWidth(0.5)
                    categories.append(QgsRendererCategory(value, symbol, str(value)))
                layer.setRenderer(QgsCategorizedSymbolRenderer(target_field, categories))

    def _validate_inputs(self):
        grid = self._grid_file.filePath()
        aoi = self._aoi_file.filePath()
        sonar = self._sonar_file.filePath()
        errors = self._errors_file.filePath()
        
        if not grid or not aoi:
            QMessageBox.warning(self, "Missing Inputs", "Please select at least Grid and AOI files.")
            return None
            
        if self.btn_all.isChecked():
            if not sonar or not errors:
                QMessageBox.warning(self, "Missing Inputs", "Please select Sonar and Errors files for 'Full Process'.")
                return None
                
        return grid, aoi, sonar, errors

    def _handle_processing(self):
        paths = self._validate_inputs()
        if not paths: return
        grid, aoi, sonar, errors = paths
        
        mode = "Full Process" if self.btn_all.isChecked() else "pending_only"

        self.date_suffix = datetime.now().strftime("%d%b%y")
        folder_suffix = self.date_suffix.upper()
        
        aoi_dir = os.path.dirname(aoi)
        self.out_dir = os.path.join(aoi_dir, f"OUTPUTDIR_{folder_suffix}")
        os.makedirs(self.out_dir, exist_ok=True)
        
        proj = QgsProject.instance()
        
        target_prefixes = ["DSW_Pending_tiles", "Dynamic_tiles", "Static_tiles", "Dynamic_tiles_with_Errors", "Dynamic_tiles_without_Errors"]
        for layer in list(proj.mapLayers().values()):
            if any(layer.name().startswith(prefix) for prefix in target_prefixes):
                proj.removeMapLayer(layer)

        self.run_button.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.time_label.setVisible(True)
        
        self.start_time = time.time()
        self.timer.start(500)

        self.task = AllInOneQaTask(
            f"Run GridXtract Process ({mode})...", 
            mode, grid, aoi, sonar, errors, 
            self.out_dir, self.date_suffix, 
            QgsProject.instance().transformContext(), 
            self._on_task_finished
        )
        self.task.progressChanged.connect(lambda val: self.progress_bar.setValue(int(val)))
        QgsApplication.taskManager().addTask(self.task)

    def _on_task_finished(self, success, error_msg, stats, mode):
        self.timer.stop()
        self.run_button.setEnabled(True)
        self.progress_bar.setValue(100)
        
        elapsed = int(time.time() - self.start_time)
        mins, secs = divmod(elapsed, 60)
        final_time = f"{mins:02d}:{secs:02d}"
        
        log_file_path = os.path.join(self.out_dir, f"Grid_process_log_{self.date_suffix}.txt")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        try:
            with open(log_file_path, "a", encoding="utf-8") as log_file:
                log_file.write(f"=== GridXtract Process Run ({mode}): {timestamp} ===\n")
                if success:
                    log_file.write(f"STATUS: SUCCESS\n")
                    log_file.write(f"TIME ELAPSED: {final_time}\n\n")
                    log_file.write(f"--- Extraction Summary ---\n")
                    log_file.write(f"Total Pending Tiles in AOI: {stats['pending']}\n\n")
                    
                    if mode == "Full Process":
                        log_file.write(f"--- Sonar Tracks Intersected Tiles ---\n")
                        log_file.write(f"Highest Track Count in a tile: {stats['max_track_count']}\n")
                        log_file.write(f"Dynamic Tiles (Sonar): {stats['dynamic']}\n")
                        log_file.write(f"Static Tiles (Non-Sonar): {stats['static']}\n\n")
                        log_file.write(f"--- Error Sonar Track Tiles ---\n")
                        log_file.write(f"Dynamic with Errors: {stats['err_with']}\n")
                        log_file.write(f"  -> Tiles with >250 tracks: {stats['err_with_250_plus']}\n")
                        log_file.write(f"Dynamic without Errors: {stats['err_no']}\n")
                        log_file.write(f"  -> Tiles with >250 tracks: {stats['err_no_250_plus']}\n")
                else:
                    log_file.write(f"STATUS: FAILED\n")
                    log_file.write(f"TIME ELAPSED: {final_time}\n\n")
                    log_file.write(f"ERROR DETAILS:\n{error_msg}\n")
                log_file.write("====================================================\n\n")
        except Exception as log_err:
            print(f"Failed to write log file: {log_err}", file=sys.stderr)

        if not success:
            self.time_label.setText(f"Process Failed  |  ⏱ {final_time}")
            QMessageBox.critical(self, "Pipeline Failed", f"An error occurred:\n{error_msg}\n\nLog saved to:\n{log_file_path}")
            return
            
        self.time_label.setText(f"GridXtract Process Finished  |  ⏱ {final_time}")

        layer_configs = [(self.task.p_pending, f"DSW_Pending_tiles_{self.date_suffix}")]
        if mode == "Full Process":
            layer_configs.extend([
                (self.task.p_dynamic, f"Dynamic_tiles_{self.date_suffix}"),
                (self.task.p_static, f"Static_tiles_{self.date_suffix}"),
                (self.task.p_err_with, f"Dynamic_tiles_with_Errors_{self.date_suffix}"),
                (self.task.p_err_no, f"Dynamic_tiles_without_Errors_{self.date_suffix}")
            ])

        proj = QgsProject.instance()
        for path, name in layer_configs:
            if os.path.exists(path):
                layer = QgsVectorLayer(path, name, "ogr")
                if layer.isValid() and layer.featureCount() > 0:
                    self._apply_symbology(layer, random_color=("Pending" in name or "Error" in name))
                    proj.addMapLayer(layer)

        summary = f"Grid Extraction Process completed successfully in {final_time}!\n\n"
        summary += f"--- Grid Extraction Summary ---\n"
        summary += f"Total Pending Tiles in AOI: {stats['pending']}\n\n"
        
        if mode == "Full Process":
            summary += f"--- Sonar Tracks Intersected Tiles ---\n"
            summary += f"Highest Track Count in a tile: {stats['max_track_count']}\n"
            summary += f"Dynamic Tiles (Sonar): {stats['dynamic']}\n"
            summary += f"Static Tiles (Non-Sonar): {stats['static']}\n\n"
            summary += f"--- Error Sonar Track Tiles ---\n"
            summary += f"Dynamic with Errors: {stats['err_with']}  (>250 tracks: {stats['err_with_250_plus']})\n"
            summary += f"Dynamic without Errors: {stats['err_no']}  (>250 tracks: {stats['err_no_250_plus']})\n\n"

        summary += f"Files saved in: {self.out_dir}"    
        
        QMessageBox.information(self, "Process Complete", summary)


class DSW_GridXtract:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dialog = None

    def initGui(self):
        icon_path = os.path.join(os.path.dirname(__file__), 'DSW_GridXtract.png')
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
        self.action = QAction(icon, "DSW GridXtract Plugin", self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToVectorMenu("&DSW GridXtract", self.action)

    def unload(self):
        if self.action:
            self.iface.removePluginVectorMenu("&DSW GridXtract", self.action)
            self.iface.removeToolBarIcon(self.action)
        if self.dialog: 
            self.dialog.close()

    def run(self):
        self.dialog = DSW_GridXtractDialog(self.iface, self.iface.mainWindow())
        self.dialog.show()
```[cite: 1]
