# -*- coding: utf-8 -*-
"""
@desc: readme
@author: 1nchaos
@time: 2023/6/2
@log: change log
"""
import os


def get_csv_path(year):
    cur_path = os.path.normpath(os.path.join(os.getcwd(), os.path.dirname(__file__)))
    return fr"{cur_path}/{year}.csv"


def _discover_years():
    cur_path = os.path.normpath(os.path.join(os.getcwd(), os.path.dirname(__file__)))
    years = []
    for name in os.listdir(cur_path):
        stem, ext = os.path.splitext(name)
        if ext.lower() != ".csv" or not stem.isdigit():
            continue
        years.append(int(stem))
    return sorted(years)


years = _discover_years()
