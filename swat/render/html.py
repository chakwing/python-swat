#!/usr/bin/env python
# encoding: utf-8

'''
Functions for rendering output in a ODS-like manner.

'''

from __future__ import print_function, division, absolute_import, unicode_literals

from IPython.display import display_html, HTML
from pprint import pformat

STYLESHEET = '''
    <style type="text/css">
    div.cas-results {
        overflow-x: auto;
    }
    div.cas-results > * {
        margin-left: auto !important;
        margin-right: auto !important;
    }
    div.cas-results .sas-dataframe th[colspan],
    div.cas-results .byline {
        text-align: center;
        font-size: 110%;
    }
    div.cas-results .sas-dataframe > tbody > tr:nth-child(even) {
        background-color: #f8f8f8;
    }
    div.cas-results .sas-dataframe td,
    div.cas-results .sas-dataframe th {
        border: 1px solid #aaaaaa;
        padding: 5px 10px;
    }
    div.cas-results .sas-dataframe th {
        background-color: #f0f0f0;
    }
    div.cas-results .sas-dataframe .double,
    div.cas-results .sas-dataframe .int32,
    div.cas-results .sas-dataframe .int64,
    div.cas-results .sas-dataframe .decsext,
    div.cas-results .sas-dataframe .decquad {
        text-align: right;
    }
    </style>
'''


def render_html(results):
    '''
    Render an ODS-like HTML report

    Parameters
    ----------
    results : CASResults object

    Returns
    -------
    None

    '''
    if hasattr(results, '_render_html_'):
        out = results._render_html_()
        if out is not None:
            return display_html(HTML(STYLESHEET + out))

    if hasattr(results, '_repr_html_'):
        out = results._repr_html_()
        if out is not None:
            return display_html(HTML(out))

    return display_html(HTML('<pre>%s</pre>' % pformat(results)))
