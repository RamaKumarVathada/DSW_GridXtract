#!/usr/bin/env python3
# -*- coding: utf-8 -*-

def classFactory(iface):
    """
    Loads the DSW_GridXtract plugin class when QGIS starts or the plugin is initialized.
    
    :param iface: A QGIS interface instance (QgisInterface)
    """
    # regardless of whether the folder is named 'DSW_GridXtract'.
    from .DSW_GridXtract import DSW_GridXtract
    return DSW_GridXtract(iface)