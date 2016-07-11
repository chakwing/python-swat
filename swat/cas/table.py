#!/usr/bin/env python
# encoding: utf-8

'''
CASTable class for interfacing with data tables in CAS

'''

from __future__ import print_function, division, absolute_import, unicode_literals

import six
import copy
import keyword
import numpy as np
import pandas as pd
import re
import sys
import uuid
import weakref
from ..config import get_option
from ..exceptions import SWATError
from ..utils import dict2kwargs, getattr_safe_property
from ..utils.compat import (int_types, binary_types, text_types, items_types,
                            patch_pandas_sort)
from ..utils.keyword import dekeywordify
from .utils.params import ParamManager, ActionParamManager
from .actions import format_params

# pylint: disable=W0212, W0221, W0613, R0904, C0330

patch_pandas_sort()

CASTABLE_DOCSTRING = '''
Create a CASTable instance

Parameters
----------
name : string
   Name of the table in CAS

**kwargs : keyword arguments, optional
   Table / output table parameters

Table Parameters
----------------
%s

Output Table Parameters
-----------------------
%s

Returns
-------
CASTable object

'''

OPERATOR_NAMES = {
    '+': 'add',
    '-': 'sub',
    '*': 'mul',
    '/': 'truediv',
    '//': 'floordiv',
    '**': 'pow',
    '%': 'mod',
    '||': 'cat',
    '>': 'gt',
    '<': 'lt',
    '>=': 'ge',
    '<=': 'le',
    '==': 'eq',
    '=':  'eq',
    '^=': 'ne',
    '!=': 'ne',
}

MAX_INT64_INDEX = 2**63 - 1 - 1  # Extra one is for 1 indexing


def _gen_table_name():
    ''' Generate a unique table name '''
    return '_PY_T_%s' % str(uuid.uuid4()).replace('-', '_').upper()


def _gen_ds_name():
    ''' Generate a unique datastep table name '''
    while True:
        name = str(uuid.uuid4()).replace('-', '').upper()
        if re.match(r'^[A-Z]', name):
            return name


def _nlit(name):
    ''' Return `name` as an nlit '''
    if re.match(r'[A-Za-z_]\w*', name):
        return name
    return '"%s"n' % _escape_string(name)


def _escape_string(val):
    ''' Escape quotes in a string '''
    return val.replace('"', '""')


def _flatten(items):
    ''' Generator to yield all nested list items '''
    for item in items:
        if isinstance(item, (list, tuple)):
            for subitem in _flatten(item):
                yield subitem
        else:
            yield item


def _get_unique(seq, lowercase=False):
    '''
    Return a list with only unique items

    Parameters
    ----------
    lowercase : boolean
        Should values be compared in a case-insensitive way?

    Returns
    -------
    list

    '''
    seen = set()
    if lowercase:
        return [x for x in seq if not (x in seen or x.lower() in seen or seen.add(x))]
    return [x for x in seq if not (x in seen or seen.add(x))]


class CASTableAccessor(object):
    ''' Base class for all accessor properties '''

    def __init__(self, table):
        self._table = weakref.ref(table)


class CASTableRowScalarAccessor(CASTableAccessor):
    ''' Implemention of the iat property '''

    def __getitem__(self, pos):
        tbl = self._table()
        if isinstance(tbl, CASColumn):
            return tbl.get_value(pos, 0)
        return tbl.get_value(*pos)


class CASTableLabelScalarAccessor(CASTableAccessor):
    ''' Implemention of the at property '''

    def __getitem__(self, pos):
        tbl = self._table()
        if isinstance(tbl, CASColumn):
            if pos < 0 or pos >= tbl._numrows:
                raise KeyError(pos)
            return tbl.get_value(pos, 0)
        if pos[0] < 0 or pos[0] >= tbl._numrows:
            raise KeyError(pos)
        return tbl.get_value(*pos)


def _get_table_selection(table, args):
    '''
    Determine the varlist, to, and from fetch parameters

    Parameters
    ----------
    table : CASTable object
        The table to index
    args : slice or tuple or int
        The argument to __getitem__

    Returns
    -------
    (dict, string)
       (Dictionary containing fetch parameters,
        String containing expected output type: table, row, column, scalar)

    '''
    cols = rowstart = rowend = None
    compvars = []
    comppgm = []
    outtype = 'table'

    # Rows and columns specified
    if isinstance(args, tuple):
        rows = args[0]

        # tbl.x[[0, 3, 2]] or tbl.x[[2]]
        if isinstance(rows, items_types):
            raise TypeError('Selecting and array of indexes is not supported')

        # tbl.x[1:12]
        elif isinstance(rows, slice):
            if rows.step and rows.step != 1:
                raise TypeError('Step values in slices are not supported')
            rowstart = rows.start
            rowend = rows.stop

        # tbl.x[10]
        elif isinstance(rows, int_types):
            rowstart = rowend = rows
            outtype = 'row'

        else:
            raise TypeError('Unknown type for row indexing: %s' % rows)

        cols = args[1]

        # tbl.x[0, ['a', 'b', 5]]
        if isinstance(cols, items_types):
            colset = set([x.lower() for x in table.columns])
            out = []
            for col in cols:
                if isinstance(col, int_types):
                    out.append(col)
                elif col.lower() in colset:
                    out.append(col)
                else:
                    out.append(col)
                    compvars.append(col)
                    comppgm.append('%s = .; ' % _nlit(col))
            cols = out

        # tbl.x[0, 'a':10]
        elif isinstance(cols, slice):
            use_names = False
            columns = None
            lowcolumns = None
            colstart, colstep, colend = cols.start, cols.step, cols.stop
            if isinstance(colstart, text_types) or isinstance(colstart, binary_types):
                if not columns:
                    columns = list(table.columns)
                    lowcolumns = [x.lower() for x in columns]
                colstart = lowcolumns.index(colstart.lower())
                use_names = True
            if isinstance(colend, text_types) or isinstance(colend, binary_types):
                if not columns:
                    columns = list(table.columns)
                    lowcolumns = [x.lower() for x in columns]
                colend = lowcolumns.index(colend.lower()) + 1
                use_names = True
            if use_names:
                cols = columns[colstart:colend:colstep]
            else:
                if colend is None:
                    if not columns:
                        columns = list(table.columns)
                    colend = len(columns)
                elif colend < 0:
                    if not columns:
                        columns = list(table.columns)
                    colend = len(columns) + colend
                cols = list(range(colend))[colstart:colend:colstep]

        # tbl.x[0, 10]
        elif isinstance(cols, int_types):
            cols = [cols]
            if outtype == 'row':
                outtype = 'scalar'
            elif outtype == 'table':
                outtype = 'column'

        # tbl.x[0, 'a']
        elif isinstance(cols, text_types) or isinstance(cols, binary_types):
            cols = [cols]
            if outtype == 'row':
                outtype = 'scalar'
            elif outtype == 'table':
                outtype = 'column'

        else:
            raise TypeError('Unknown type for column indexing: %s' % cols)

    # Range of rows specified
    elif isinstance(args, slice):
        if args.step and args.step != 1:
            raise TypeError('Step values in row slices are not supported')
        rowstart = args.start or 0
        rowend = args.stop or (table._numrows - 1)
        cols = None

    # One row specified
    elif isinstance(args, int_types):
        rowstart = args
        rowend = args
        cols = None
        outtype = 'row'

    else:
        raise TypeError('Unknown type for table indexing: %s' % args)

    # Set missing endpoints
    if rowstart is None:
        rowstart = 0
    if rowend is None:
        rowend = table._numrows - 1

    out = {'start': rowstart, 'stop': rowend}

    if cols:
        out['table.varlist'] = cols

    if compvars and comppgm:
        out['table.compvars'] = compvars
        out['table.comppgm'] = comppgm

    # TODO: Set table.where for row label indexing when
    #       a table index is supported.

    return out, outtype


class CASTableAnyLocationAccessor(CASTableAccessor):
    ''' Implementation of the ix property '''
    accessor = 'ix'

    def __getitem__(self, pos):
        numrows = None

        is_column = isinstance(self._table(), CASColumn)
        if is_column:
            if isinstance(pos, tuple):
                raise TypeError('Too many indexers')
            pos = tuple([pos, 0])

        params, dtype = _get_table_selection(self._table(), pos)

        if dtype == 'column' or dtype == 'table':
            raise TypeError('Index must result in either a scalar or a row')

        # With numeric column index, only row labels are allowed
        if type(self).accessor == 'ix':
            if params['start'] < 0:
                raise KeyError(params['start'])
            if params['stop'] < 0:
                raise KeyError(params['stop'])

        tbl = self._table()

        # Set negative index values
        if params['start'] < 0:
            numrows = tbl._numrows
            params['start'] = params['start'] + numrows
        if params['stop'] < 0:
            if numrows is None:
                numrows = tbl._numrows
            params['stop'] = params['stop'] + numrows

        if 'table.varlist' in params or 'table.compvars' in params:
            tbl = tbl.copy()

        # Build varlist
        table_varlist = None
        if 'table.varlist' in params:
            varlist = []
            table_varlist = params.pop('table.varlist')
            columns = None
            for item in table_varlist:
                if isinstance(item, int_types):
                    if not columns:
                        columns = list(tbl.columns)
                    varlist.append(columns[item])
                else:
                    varlist.append(item)
            tbl.set_param('varlist', varlist)

        # Append compvars and comppgm
        if 'table.compvars' in params and 'table.comppgm' in params:
            tbl.append_computed_columns(params.pop('table.compvars'),
                                        params.pop('table.comppgm'))

        # Reformat params for table.fetch
        fetch_params = {'from': params['start'] + 1, 'to': params['stop']}

        from_ = params['start']

        # Return a single value
        if dtype == 'scalar':
            out = pd.concat(list(tbl._retrieve('table.fetch', sastypes=False,
                                               noindex=True, **fetch_params).values()))
            if len(out):
                out.index = [from_]
            if type(self).accessor == 'iloc':
                if table_varlist and not isinstance(table_varlist[0], int_types):
                    return out.iloc[0, table_varlist[0]]
                return out.iloc[0, 0]
            elif type(self).accessor == 'ix':
                return getattr(out, type(self).accessor)[from_, 0]
            return getattr(out, type(self).accessor)[from_, table_varlist[0]]

        # Return a series
        if dtype == 'row':
            out = pd.concat(list(tbl._retrieve('table.fetch', sastypes=False,
                                               noindex=True, **fetch_params).values()))
            if len(out):
                out.index = [from_]
            if type(self).accessor == 'iloc':
                if is_column:
                    return out.iloc[0].tolist()[0]
                return out.iloc[0]
            if is_column:
                return getattr(out, type(self).accessor)[from_].tolist()[0]
            return getattr(out, type(self).accessor)[from_]

        return tbl


class CASTableRowLocationAccessor(CASTableAnyLocationAccessor):
    ''' Implementation of the iloc property '''
    accessor = 'iloc'


class CASTableLabelLocationAccessor(CASTableAnyLocationAccessor):
    ''' Implementation of the loc property '''
    accessor = 'loc'


class CASTablePlotter(object):
    '''
    Plotting class for CASTable

    Parameters
    ----------
    table : CASTable object
       The CASTable object to bind to

    Returns
    -------
    CASTablePlotter object

    '''

    def __init__(self, table):
        self._table = table

    def __call__(self, *args, **kwargs):
        '''
        Make a line plot of all columns in a table

        See Also: pandas.DataFrame.plot (for arguments)

        '''
        return self._table._fetch(grouped=True).plot(*args, **kwargs)

    def area(self, *args, **kwargs):
        '''
        Area plot

        See Also: pandas.DataFrame.area (for arguments)

        '''
        return self._table._fetch(grouped=True).plot.area(*args, **kwargs)

    def bar(self, *args, **kwargs):
        '''
        Bar plot

        See Also: pandas.DataFrame.bar (for arguments)

        '''
        return self._table._fetch(grouped=True).plot.bar(*args, **kwargs)

    def barh(self, *args, **kwargs):
        '''
        Horizontal bar plot

        See Also: pandas.DataFrame.barh (for arguments)

        '''
        return self._table._fetch(grouped=True).plot.barh(*args, **kwargs)

    def box(self, *args, **kwargs):
        '''
        Boxplot

        See Also: pandas.DataFrame.box (for arguments)

        '''
        return self._table._fetch(grouped=True).plot.box(*args, **kwargs)

    def density(self, *args, **kwargs):
        '''
        Kernel density estimate plot

        See Also: pandas.DataFrame.density (for arguments)

        '''
        return self._table._fetch(grouped=True).plot.density(*args, **kwargs)

    def hexbin(self, *args, **kwargs):
        '''
        Hexbin plot

        See Also: pandas.DataFrame.hexbin (for arguments)

        '''
        return self._table._fetch(grouped=True).plot.hexbin(*args, **kwargs)

    def hist(self, *args, **kwargs):
        '''
        Histogram

        See Also: pandas.DataFrame.histogram (for arguments)

        '''
        return self._table._fetch(grouped=True).plot.hist(*args, **kwargs)

    def kde(self, *args, **kwargs):
        '''
        Kernel density estimate plot

        See Also: pandas.DataFrame.kde (for arguments)

        '''
        return self._table._fetch(grouped=True).plot.kde(*args, **kwargs)

    def line(self, *args, **kwargs):
        '''
        Line plot

        See Also: pandas.DataFrame.line (for arguments)

        '''
        return self._table._fetch(grouped=True).plot.line(*args, **kwargs)

    def pie(self, *args, **kwargs):
        '''
        Pie chart

        See Also: pandas.DataFrame.pie (for arguments)

        '''
        return self._table._fetch(grouped=True).plot.pie(*args, **kwargs)

    def scatter(self, *args, **kwargs):
        '''
        Scatter plot

        See Also: pandas.DataFrame.pie (for arguments)

        '''
        return self._table._fetch(grouped=True).plot.scatter(*args, **kwargs)


@six.python_2_unicode_compatible
class CASTable(ParamManager, ActionParamManager):
    # Docstring purposely left blank.  It will be populated dynamically.

    table_params = set()
    outtable_params = set()
    all_params = set()

    getdoc = None

    def __init__(self, name, **kwargs):
        ParamManager.__init__(self, name=name, **kwargs)
        ActionParamManager.__init__(self)
        self._connection = None
        self._contexts = []

        self._iat = CASTableRowScalarAccessor(self)
        self._at = CASTableLabelScalarAccessor(self)
        self._iloc = CASTableRowLocationAccessor(self)
        self._loc = CASTableLabelLocationAccessor(self)
        self._ix = CASTableAnyLocationAccessor(self)

        self._plot = CASTablePlotter(self)

        self._dir = set([x for x in self.__dict__.keys() if not x.startswith('_')])
        self.params.set_dir_values(type(self).all_params)

        # Add doc to params
        init = self.__init__
        if hasattr(init, '__func__'):
            init = init.__func__
        if init is not None and init.__doc__ is not None:
            doc = 'Table Parameters' + init.__doc__.split('Table Parameters', 1)[-1]
            doc = doc.split('Returns')[0].rstrip()
            self.params.set_doc(doc)

    def append_varlist(self, *items, **kwargs):
        '''
        Append variable names to varlist parameter

        Parameters
        ----------
        *items : strings or lists-of-strings
            Names to append.
        inplace : boolean, optional
            If True, the current varlist is appended.
            If False, the new varlist is returned.

        Returns
        -------
        None
            if inplace=True
        list of strings
            if inplace=False

        '''
        varlist = self.get_param('varlist', [])
        if not varlist:
            varlist = list(self.columns)
        if not isinstance(varlist, items_types):
            varlist = [varlist]
        for item in _flatten(items):
            if item:
                varlist.append(item)
        varlist = _get_unique(varlist, lowercase=True)
        if kwargs.get('inplace', True):
            self.set_param('varlist', varlist)
            return
        return varlist

    def append_compvars(self, *items, **kwargs):
        '''
        Append variable names to compvars parameter

        Parameters
        ----------
        *items : strings or lists-of-strings
            Names to append.
        inplace : boolean, optional
            If True (the default), the current compvars is appended.
            If False, the new compvars is returned.

        Returns
        -------
        None
            if inplace=True
        list of strings
            if inplace=False

        '''
        varlist = self.get_param('compvars', [])
        if not isinstance(varlist, items_types):
            varlist = [varlist]
        for item in _flatten(items):
            if item:
                varlist.append(item)
        varlist = _get_unique(varlist, lowercase=True)
        if kwargs.get('inplace', True):
            self.set_param('compvars', varlist)
            return
        return varlist

    def append_groupby(self, *items, **kwargs):
        '''
        Append variable names to groupby parameter

        Parameters
        ----------
        *items : strings or lists-of-strings
            Names to append.
        inplace : boolean, optional
            If True (the default), the current groupby is appended.
            If False, the new groupby is returned.

        Returns
        -------
        None
            if inplace=True
        list of strings
            if inplace=False

        '''
        varlist = self.get_param('groupby', [])
        if not isinstance(varlist, items_types):
            varlist = [varlist]
        for item in _flatten(items):
            if item:
                varlist.append(item)
        varlist = _get_unique(varlist, lowercase=True)
        if kwargs.get('inplace', True):
            self.set_param('groupby', varlist)
            return
        return varlist

    def append_comppgm(self, *items, **kwargs):
        '''
        Append code to comppgm parameter

        Parameters
        ----------
        *items : strings or lists-of-strings
            Code to append.
        inplace : boolean, optional
            If True, the current comppgm is appended.
            If False, the new comppgm is returned.

        Returns
        -------
        None
            if inplace=True
        string
            if inplace=False

        '''
        code = self.get_param('comppgm', [])
        if not isinstance(code, items_types):
            code = [code]
        for item in _flatten(items):
            if item:
                code.append(item)
        for i, block in enumerate(code):
            if not re.search(r';\s*$', block):
                code[i] = '%s; ' % block.rstrip()
        code = ''.join(_get_unique(code))
        if kwargs.get('inplace', True):
            self.set_param('comppgm', code)
            return
        return code

    def append_computed_columns(self, names, code, inplace=True):
        '''
        Append computed columns as specified

        Parameters
        ----------
        names : string or list-of-strings
            Names of computed columns.
        code : string or list-of-strings
            Code blocks for computed columns.
        inplace : boolean, True
            If True, the computed column specification is appended.
            If False, the computed column specification is returned.

        Returns
        -------
        None
            if inplace=True
        (compvars, comppgm)
            if inplace=False

        '''
        out = (self.append_compvars(names, inplace=inplace),
               self.append_comppgm(code, inplace=inplace))
        if inplace:
            return
        return out

    def append_where(self, *items, **kwargs):
        '''
        Append code to where parameter

        Parameters
        ----------
        *items : strings or lists-of-strings
            Code to append.
        inplace : boolean, optional
            If True, the current where is appended.
            If False, the new where is returned.

        Returns
        -------
        None
            if inplace=True
        string
            if inplace=False

        '''
        code = self.get_param('where', [])
        if not isinstance(code, items_types):
            code = [code]
        for item in _flatten(items):
            if item:
                code.append(item)
        code = ' and '.join(_get_unique(['(%s)' % x for x in code if x.strip()]))
        if kwargs.get('inplace', True):
            self.set_param('where', code)
            return
        return code

    def append_orderby(self, *items, **kwargs):
        '''
        Append orderby parameters

        Parameters
        ----------
        *items : strings or dicts or list-of-strings or list-of-dicts
            Sorting parameters.  Each item can be a name of a column,
            or a dictionary containing 'name', 'order', and 'formatted' keys.
        inplace : boolean, optional
            If True (the default), the orderby parameter is appended.
            If False, the new orderby parameter is returned.

        Returns
        -------
        None
            if inplace=True
        list of dicts
            if inplace=False

        '''
        name = 'orderby'
        orderby = []

        # See if there is a orderby or sortby
        if self.has_param('orderby'):
            orderby = self.get_param('orderby')
        elif self.has_param('sortby'):
            name = 'sortby'
            orderby = self.get_param('sortby')

        # Force orderby to be a list
        if isinstance(orderby, dict):
            orderby = [orderby]
        elif isinstance(orderby, items_types):
            orderby = list(orderby)
        else:
            orderby = [dict(name=orderby)]

        # Append sort parameters
        for item in items:
            if not item:
                continue
            if isinstance(item, text_types) or isinstance(item, binary_types):
                orderby.append(dict(name=item))
            elif isinstance(item, dict):
                orderby.append(item)
            else:
                for subitem in item:
                    if not subitem:
                        continue
                    if isinstance(subitem, text_types) or \
                            isinstance(subitem, binary_types):
                        orderby.append(dict(name=subitem))
                    else:
                        orderby.append(subitem)

        # Set it as needed
        if kwargs.get('inplace', True):
            self.set_param(name, orderby)
            return

        return orderby

    def _to_column(self, varname=None):
        '''
        Convert CASTable to CASColumn

        Parameters
        ----------
        varname : string, optional
            The name of the column to use.  If not specified, the first
            column in the table is used.

        Returns
        -------
        CASColumn object

        '''
        table = copy.deepcopy(self)

        params = table.to_params()
        if varname is not None:
            params['varlist'] = [varname]
        else:
            varlist = params.get('varlist', [])
            if isinstance(varlist, items_types) and len(varlist):
                params['varlist'] = [varlist[0]]

        column = CASColumn(**params)

        for key, value in six.iteritems(table._action_params):
            column._action_params[key] = value

        try:
            column.set_connection(table.get_connection())
        except SWATError:
            pass

        return column

    def __dir__(self):
        try:
            conn = self.get_connection()
            return list(self._dir) + list(conn.get_action_names())
        except:
            pass
        return list(self._dir)

    @classmethod
    def _bootstrap(cls, connection):
        '''
        Populate documentation and method signatures

        Parameters
        ----------
        connection : CAS instance
           CAS connection to use for reflection

        Returns
        -------
        None

        '''
        if not cls.table_params or not cls.outtable_params:
            tblparams = 'Unknown'
            outtblparams = 'Unknown'

            param_names = []

            actinfo = connection._get_action_info('builtins.cascommon')
            for item in actinfo[-1].get('params'):
                if 'parmList' in item:
                    # Populate valid fields for tables and outtables
                    if item['name'] == 'castable':
                        cls.table_params = set([x['name'] for x in item['parmList']])
                        tblparams = format_params(item['parmList'], connection,
                                                  param_names=param_names).rstrip()

                    elif item['name'] == 'casouttable':
                        cls.outtable_params = set([x['name'] for x in item['parmList']])
                        outtblparams = format_params(item['parmList'], connection,
                                                     param_names=param_names).rstrip()

            for name in list(param_names):
                if keyword.iskeyword(name):
                    param_names.append(dekeywordify(name))

            cls.all_params = set(param_names)

            cls.param_names = cls.table_params.union(cls.outtable_params)

            init = cls.__init__
            if hasattr(init, '__func__'):
                init = init.__func__
            init.__doc__ = CASTABLE_DOCSTRING % (tblparams, outtblparams)

    def set_connection(self, connection):
        '''
        Set the connection to use for action calls

        Parameters
        ----------
        connection : CAS object
            The connection object to use

        Note: This method creates a weak reference to the connection.
              If the connection is released, actions will no longer
              be able to be called from the CASTable object.

        Returns
        -------
        None

        '''
        if connection is None:
            self._connection = None
        else:
            self._connection = weakref.ref(connection)

    def get_connection(self):
        '''
        Get the connection object

        Raises
        ------
        SWATError
            If no connection is available either because it wasn't
            set, or because the connection object was released.

        Returns
        -------
        CAS object
            If a connection exists on the CASTable

        '''
        conn = None
        if self._connection is not None:
            try:
                conn = self._connection()
            except SWATError:
                pass
        if conn is None:
            raise SWATError('No connection is currently registered')
        return conn

    def __eq__(self, other):
        ''' Test for equality '''
        if isinstance(other, CASTable):
            if self.params == other.params:
                return True
        return False

    def __copy__(self):
        '''
        Make a shallow copy of the CASTable object

        Returns
        -------
        CASTable object
           Shallow copy of `self`

        '''
        tbl = type(self)(**self.params)
        tbl._action_params = {}
        for key, value in six.iteritems(self._action_params):
            tbl._action_params[key] = copy.copy(value)
        try:
            tbl.set_connection(self.get_connection())
        except SWATError:
            pass
        return tbl

    def __deepcopy__(self, memo):
        '''
        Make a deep copy of the CASTable obect

        Parameters
        ----------
        memo : any
           Storage for deepcopy mechanism

        Returns
        -------
        CASTable object
           Deep copy of `self`

        '''
        tbl = type(self)(**copy.deepcopy(self.params))
        tbl._action_params = copy.deepcopy(self._action_params)
        try:
            tbl.set_connection(self.get_connection())
        except SWATError:
            pass
        return tbl

    def __setattr__(self, name, value):
        ''' Set attribute value '''
        return super(CASTable, self).__setattr__(name.lower(), value)

    def __delattr__(self, name):
        ''' Delete attribute '''
        return super(CASTable, self).__delattr__(name.lower())

    def __getattr__(self, name):
        ''' Get named attribute '''
        origname = name
        name = name.lower()

        # Short circuit any table attributes
        if '.' not in name:
            try:
                return super(CASTable, self).__getattr__(name)
            except AttributeError:
                pass

        conn = self.get_connection()

        if '.' not in name:
            if not(re.match(r'^[A-Z]', origname)) and conn.has_actionset(name):
                asinst = conn.get_actionset(name)
                asinst.default_params = {'table': self.to_params()}
                return asinst

        if conn.has_action(name):
            actcls = conn.get_action_class(name)
            members = {'default_params': {'table': copy.deepcopy(self)}}
            actcls = type(actcls.__name__, (actcls,), members)

            if re.match(r'^[A-Z]', origname):
                return actcls

            return actcls()

        # See if it's a column name
        if name in [x.lower() for x in self.get_param('varlist', [])]:
            return self._to_column(origname)
        elif name in [x.lower() for x in self.get_param('compvars', [])]:
            return self._to_column(origname)
        elif not self.get_param('varlist', []):
            try:
                tbl = self.copy()
                tbl.set_param('varlist', [origname])
                colinfo = tbl._columninfo
            except (ValueError, SWATError):
                colinfo = None
            if colinfo is not None and len(colinfo):
                return self._to_column(origname)

        raise AttributeError(origname)

    def invoke(self, _name_, **kwargs):
        '''
        Invoke an action on the registered connection

        Parameters
        ----------
        _name_ : string
            Action name
        **kwargs : any, optional
            Keyword arguments to the action

        Returns
        -------
        CAS object
            The CAS object that the action was executed on

        '''
        return getattr(self, _name_).invoke(**kwargs)

    def retrieve(self, _name_, **kwargs):
        '''
        Invoke an action on the registered connection and retrieve results

        Parameters
        ----------
        _name_ : string
            Action name
        **kwargs : any, optional
            Keyword arguments to the action

        Returns
        -------
        CASResults object

        '''
        return getattr(self, _name_).retrieve(**kwargs)

    def _retrieve(self, _name_, **kwargs):
        ''' Same as retrieve, but marked as a UI call '''
        out = self.retrieve(_name_, _apptag='UI', _messagelevel='none', **kwargs)
        if out.severity > 1:
            raise SWATError(out.status)
        return out

    def __str__(self):
        parts = [
            repr(self.params['name']),
            dict2kwargs(self.to_params(), ignore=['name'], fmt='%s')
        ]

        # Check for sort parameters
        sort = ''
        fetch = self.get_action_params('table.fetch')
        if fetch and ('sortby' in fetch or 'orderby' in fetch):
            sortby = fetch.get('sortby', fetch.get('orderby'))
            if isinstance(sortby, dict):
                sortby = [sortby]
            elif not isinstance(sortby, items_types):
                sortby = [dict(name=sortby, ascending=True)]

            names = [x['name'] for x in sortby]
            if len(names) == 1:
                names = names.pop()

            order = [x.get('order', 'ASCENDING').upper() == 'ASCENDING' for x in sortby]
            if set(order) == set([True]):
                order = ''
            elif set(order) == set([False]):
                order = ', ascending=False'
            else:
                order = ', ascending=%s' % repr(order)

            sort = '.sort_values(%s%s)' % (repr(names), order)

        return '%s(%s)%s' % (type(self).__name__,
                             ', '.join([x.strip() for x in parts if x.strip()]),
                             sort)

    def __repr__(self):
        return str(self)

    def get_action_names(self):
        ''' Return a list of available actions '''
        return self.get_connection().get_action_names()

    def get_actionset_names(self):
        ''' Return a list of available actionsets '''
        return self.get_connection().get_actionset_names()

    def to_table_params(self):
        '''
        Create a copy of the table using only input table parameters

        Returns
        -------
        CASTable object
           Table object with only input table parameters

        '''
        if type(self).table_params:
            out = {}
            for key in self.params.keys():
                if key.lower() in type(self).table_params:
                    out[key] = copy.deepcopy(self.params[key])
            return out

        # This can only happen if the table_params class variable
        # wasn't populated when the server connection was made,
        # which should *never* happen.
        out = copy.deepcopy(self.params)
        return out

    def to_table(self):
        '''
        Create a copy of the table with only input table paramaters

        Returns
        -------
        CASTable object

        '''
        kwargs = self.to_table_params()
        name = kwargs.pop('name')
        out = type(self)(name, **kwargs)
        try:
            out.set_connection(self.get_connection())
        except SWATError:
            pass
        return out

    def to_outtable_params(self):
        '''
        Create a copy of the table using only the output table parameters

        Returns
        -------
        CASTable object
           Table object with only output table parameters

        '''
        if type(self).outtable_params:
            out = {}
            for key in self.params.keys():
                if key.lower() in type(self).outtable_params:
                    out[key] = copy.deepcopy(self.params[key])
            return out

        # This can only happen if the table_params class variable
        # wasn't populated when the server connection was made,
        # which should *never* happen.
        out = copy.deepcopy(self.params)
        return out

    def to_outtable(self):
        '''
        Create a copy of the table with only output table paramaters

        Returns
        -------
        CASTable object

        '''
        kwargs = self.to_outtable_params()
        name = kwargs.pop('name')
        out = type(self)(name, **kwargs)
        try:
            out.set_connection(self.get_connection())
        except SWATError:
            pass
        return out

    def to_table_name(self):
        '''
        Return name of table for isTableName parameters

        Returns
        -------
        string
           CASTable name

        '''
        return self.params['name']

    #
    # Pandas DataFrame API
    #

    # Attributes and underlying data

    def _loadactionset(self, name):
        ''' Load action set if it hasn't been loaded already '''
        if not hasattr(self, name):
            self._retrieve('builtins.loadactionset', actionset=name)

    @getattr_safe_property
    def _columninfo(self):
        ''' Return columninfo dataframe '''
        return self._retrieve('table.columninfo')['ColumnInfo']

    @getattr_safe_property
    def _numrows(self):
        ''' Return number of rows in the table '''
        return self._retrieve('simple.numrows')['numrows']

    def __len__(self):
        return self._numrows

    # NOTE: Workaround to keep the DataFrame text renderer from trying
    #       to fetch all the values in the table.
    def __next__(self):
        return StopIteration

    # NOTE: Workaround to keep the DataFrame text renderer from trying
    #       to fetch all the values in the table.
    def next(self):
        ''' Return next item in the iteration '''
        return StopIteration

    @getattr_safe_property
    def _numcolumns(self):
        ''' Return number of visible columns '''
        # Short circuit if we can
        varlist = self.get_param('varlist', [])
        if varlist:
            return len(varlist)

        # Call tableinfo
        tblinfo = self._retrieve('table.tableinfo')['TableInfo']
        compvars = self.get_param('compvars', [])
        if compvars and not isinstance(compvars, items_types):
            compvars = [compvars]
        return tblinfo.ix[0, 'Columns'] + len(compvars)

    @getattr_safe_property
    def columns(self):
        ''' Return names of the visible columns in the table '''
        varlist = self.get_param('varlist', [])
        if varlist:
            return pd.Index(varlist)
        return pd.Index(self._columninfo['Column'].tolist())

    @getattr_safe_property
    def index(self):
        ''' Return the table index '''
        return

    def _intersect_varlist(self, columns, inplace=False):
        ''' Return the intersection of `columns` and `self.varlist` '''
        if columns is None:
            columns = []
        elif not isinstance(columns, items_types):
            columns = [columns]

        if not columns:
            if inplace:
                return
            return self.get_param('varlist', [])

        varlist = self.get_param('varlist', [])
        if not varlist:
            varlist = columns
        else:
            varlist = list(sorted(set(varlist) & set(columns), key=varlist.index))

        if inplace:
            self.set_param('varlist', varlist)
            return

        return varlist

    def as_matrix(self, columns=None, n=None):
        '''
        Convert the CASTable to its Numpy-array representation

        Parameters
        ----------
        columns : list of strings, optional
            The names of the columns to add to the matrix
        n : int or long, optional
            The maximum number of rows to fetch.  If None, then the value
            in swat.options.dataset.max_rows_fetched is used.

        Returns
        -------
        numpy.array

        '''
        if n is None:
            n = get_option('cas.dataset.max_rows_fetched')
        tbl = self.copy(deep=True)
        tbl._intersect_varlist(columns, inplace=True)
        return pd.concat(list(tbl._retrieve('table.fetch', to=n, noindex=True,
                                            sastypes=False).values())).as_matrix()

    @getattr_safe_property
    def dtypes(self):
        ''' Return the data types in the table '''
        out = self._columninfo.copy()
        index = out['Column']
        index.name = None
        out = out['Type']
        out.index = index
        out.name = None
        return out

    @getattr_safe_property
    def ftypes(self):
        ''' Return the ftypes (indication of sparse/dense and dtype) in the table '''
        out = self._columninfo.copy()
        index = out['Column']
        index.name = None
        out = out['Type'] + ':dense'
        out.index = index
        out.name = None
        return out

    def get_dtype_counts(self):
        ''' Retrieve the frequency of CAS table column data types '''
        return self.dtypes.value_counts().sort_index()

    def get_ftype_counts(self):
        ''' Retrieve the frequency of CAS table column data types '''
        return self.ftypes.value_counts().sort_index()

    def select_dtypes(self, include=None, exclude=None):
        '''
        Return a subset CASTable including/excluding columns based on data type

        Parameters
        ----------
        include : list-of-strings, optional
            List of data type names to include in result
        exclude : list-of-strings, optional
            List of data type names to exclude from result

        Note
        ----
        In addition to data type names, the names 'number' and 'numeric'
        can be used to refer to all numeric types.  The name 'character'
        can be used to refer to all character types.

        Numerics can also be referred to by the names 'integer' and
        'floating' to refer to integers and floating point types,
        respectively.

        Returns
        -------
        CASTable object

        '''
        if include is None:
            include = set()
        elif not isinstance(include, items_types):
            include = [include]
        include = set(include)

        if exclude is None:
            exclude = set()
        elif not isinstance(exclude, items_types):
            exclude = [exclude]
        exclude = set(exclude)

        out = self._columninfo.copy()
        names = out['Column'].tolist()
        dtypes = out['Type'].tolist()

        char_types = set(['char', 'varchar', 'binary', 'varbinary'])
        num_types = set(dtypes).difference(char_types)
        integer_types = set(['int32', 'int64', 'date', 'time', 'datetime'])
        float_types = num_types.difference(integer_types)

        if 'character' in include or 'O' in include or object in include:
            include.update(char_types)
        if 'number' in include or 'numeric' in include or np.number in include:
            include.update(num_types)
        if 'floating' in include:
            include.update(float_types)
        if 'integer' in include:
            include.update(integer_types)

        if 'character' in exclude or 'O' in exclude or object in exclude:
            exclude.update(char_types)
        if 'number' in exclude or 'numeric' in exclude or np.number in exclude:
            exclude.update(num_types)
        if 'floating' in exclude:
            exclude.update(float_types)
        if 'integer' in exclude:
            exclude.update(integer_types)

        varlist = set()

        if include:
            for name, dtype in zip(names, dtypes):
                if dtype in include:
                    varlist.add(name)
        else:
            varlist = set(names)

        if exclude:
            for name, dtype in zip(names, dtypes):
                if dtype in exclude:
                    varlist.discard(name)

        tblcopy = copy.deepcopy(self)
        tblcopy.set_param('varlist', [x for x in names if x in varlist])

        return tblcopy

    @getattr_safe_property
    def values(self):
        ''' Numy representation of the table '''
        return self._fetch().values

    @getattr_safe_property
    def axes(self):
        ''' Return a list with the row axis labels and column axis labels '''
        # TODO: Create an index proxy object
        return [[], self.columns]

    @getattr_safe_property
    def ndim(self):
        ''' Number of axes dimensions '''
        return 2

    @getattr_safe_property
    def size(self):
        ''' Number of elements in the table '''
        shape = self.shape
        return shape[0] * shape[1]

    @getattr_safe_property
    def shape(self):
        ''' Return a tuple representing the dimensionality of the table '''
        return self._numrows, self._numcolumns

    # Conversion

#   def astype(self, *args, **kwargs):
#       ''' Cast the table to the specified data type '''
#       raise NotImplementedError

#   def convert_objects(self, *args, **kwargs):
#       ''' Deprecated '''
#       raise NotImplementedError

    def copy(self, deep=False):
        '''
        Make a copy of the CASTable object

        Parameters
        ----------
        deep : boolean, optional
            Should all list / dict-type objects be deep copied?

        Returns
        -------
        CASTable object

        '''
        if deep:
            return copy.deepcopy(self)
        return copy.copy(self)

#   def isnull(self):
#       # TODO: Should build where clause that creates a table of booleans
#       #       where missings are True and non-missings are False.
#       raise NotImplementedError

#   def notnull(self):
#       # TODO: Same as above but reverse boolean values
#       raise NotImplementedError

    # Indexing, iteration

    def head(self, n=5, columns=None):
        ''' Retrieve the specified number of rows '''
        return self.slice(start=0, stop=n - 1, columns=columns)

    def tail(self, n=5, columns=None):
        ''' Retrieve the specified number of rows from the end '''
        return self.slice(start=-n, stop=-1, columns=columns)

    def slice(self, start=0, stop=None, columns=None):
        ''' Retrieve the specified rows '''
        tbl = self

        if columns is not None:
            tbl = self.copy()
            tbl.set_param('varlist', list(columns))

        groups = self.get_groupby_vars()
        if groups:
            groups = [x[1] for x in tbl.groupby(groups)]
        else:
            groups = [tbl]

        out = []
        for group in groups:
            if stop is None:
                stop = start + 5

            if start < 0 or stop < 0:
                numrows = group._numrows
                if start < 0:
                    start = numrows + start
                if stop < 0:
                    stop = numrows + stop

            out.append(pd.concat(list(group._retrieve('table.fetch', sastypes=False,
                                                      from_=start + 1, to=stop + 1).values())))

        out = pd.concat(out)

        try:
            out.set_index('_Index_')
            out.index.name = None
        except KeyError:
            pass

        return out

#
# TODO: Until CAS tables get index support (or we fake it locally) we
#       can't implement the following methods properly.  We might be able to use
#       set_index to store a column name that is treated as the index of
#       the table and use that for the following methods.
#

    @getattr_safe_property
    def at(self):
        ''' Label-based scalar accessor '''
        return self._at

    @getattr_safe_property
    def iat(self):
        ''' Integer location scalar accessor '''
        return self._iat

    @getattr_safe_property
    def ix(self):
        ''' Label-based indexer with integer position fallback '''
        return self._ix

    @getattr_safe_property
    def loc(self):
        ''' Label-based indexer '''
        return self._loc

    @getattr_safe_property
    def iloc(self):
        ''' Integer location based indexing for selection by position '''
        return self._iloc

    def xs(self, key, axis=0, level=None, copy=None, drop_level=True):
        '''
        Return a cross-section from the CASTable

        Parameters
        ----------
        key : string or int
            Label contained in the index
        axis : int, optional
            Axis to retrieve from (0=row, 1=column)
        level : object
            Unsupported
        copy : boolean
            Unsupported
        drop_level : boolean
            Unsupported

        Returns
        -------
        Series for axis=0 indexing
        CASColumn for axis=1 indexing

        '''
        if axis == 0:
            return self.loc[key]
        if axis == 1:
            return self._to_column(key)
        raise SWATError('Unsupported axis: %s' % axis)

#   def insert(self, *args, **kwargs):
#       raise NotImplementedError

    def __iter__(self):
        for col in self.columns:
            yield col

    def iteritems(self):
        ''' Iterate over column names '''
        for col in self.columns:
            yield (col, self._to_column(col))

    def _generic_iter(self, name, *args, **kwargs):
        ''' Generic iterator for various iteration implementations '''
        kwargs = kwargs.copy()

        has_index = 'index' in kwargs
        index = kwargs.pop('index', True)

        chunksize = kwargs.pop('chunksize', None)
        if chunksize is None:
            chunksize = 200

        # Remove index, we apply it ourselves
        if has_index:
            kwargs['index'] = False

        iterrows = name == 'iterrows' and True or False

        start = 1
        stop = chunksize

        i = 0
        while True:
            out = pd.concat(list(self._retrieve('table.fetch', from_=start, to=stop,
                                                sastypes=False, noindex=True).values()))

            if not len(out):
                break

            for item in getattr(out, name)(*args, **kwargs):
                # iterrows
                if iterrows:
                    yield (i, item[1])

                # itertuples with index
                elif index:
                    item = list(item)
                    item.insert(0, i)
                    yield tuple(item)

                # everything else
                else:
                    yield item

                i += 1

            start = stop + 1
            stop = start + chunksize

    def iterrows(self, chunksize=None):
        ''' Iterate over rows '''
        return self._generic_iter('iterrows', chunksize=chunksize)

    def itertuples(self, index=True, chunksize=None):
        ''' Iterate over rows as tuples '''
        return self._generic_iter('itertuples', index=index, chunksize=chunksize)

    def get_value(self, index, col, **kwargs):
        ''' Retrieve a single scalar value '''
        if isinstance(col, int_types):
            col = self.columns[col]
        tbl = self.copy()
        tbl.set_param('varlist', [col])
        numrows = self._numrows
        if abs(index) >= numrows:
            raise IndexError('index %s is out of bounds for axis 0 with size %s' %
                             (index, numrows))
        if index < 0:
            index = index + numrows
        out = self._fetch(from_=index + 1, to=index + 1)
        return out.get_value(0, col, **kwargs)

    def lookup(self, row_labels, col_labels):
        ''' Retrieve values indicated by row_labels, col_labels positions '''
        data = []
        for row, col in zip(row_labels, col_labels):
            data.append(self.get_value(row, col))

        types = set([type(x) for x in data])
        out = None
        if len(types) == 1:
            try:
                out = np.ndarray(shape=(len(row_labels),), dtype=types.pop())
            except ValueError:
                pass

        if out is None:
            out = np.ndarray(shape=(len(row_labels),), dtype=object)

        out[:] = data

        return out

    def pop(self, colname):
        '''
        Remove a column from the CASTable and return it

        Parameters
        ----------
        colname : string
            Name of the column to remove.

        Returns
        -------
        CASColumn object

        '''
        out = self.copy(deep=True)._to_column(colname)
        del self[colname]
        return out

    def __delitem__(self, colname):
        ''' Remove a column from the CASTable '''
        varlist = self.columns.tolist()
        lcolname = colname.lower()
        newvarlist = [x for x in varlist if x.lower() != lcolname]
        if len(newvarlist) == len(varlist):
            raise KeyError(colname)
        self.set_param('varlist', newvarlist)

    def datastep(self, code, casout=None, *args, **kwargs):
        '''
        Execute Data step code against the CAS table

        Parameters
        ----------
        code : string
            The Data step code to execute.
        casout : dict, optional
            The name and caslib of the output table.
        *args : any, optional
            Arbitrary positional arguments to the datastep.runcode action
        **kwargs : any, optional
            Arbitrary keyword arguments to the datastep.runcode action

        Returns
        -------
        CASResults object

        '''
        view = self.to_view(name=_gen_ds_name())
        if casout is None:
            casout = {'name': _gen_ds_name()}
        elif isinstance(casout, text_types) or isinstance(casout, binary_types):
            casout = {'name': casout}
        outdata = casout['name']
        if 'caslib' in casout:
            outdata = '%s(caslib=%s)' % (outdata, casout['caslib'])
        code = 'data %s;\n   set %s;\n %s;\nrun;' % (outdata,
                                                     view.get_param('name'),
                                                     code)
        kwargs = kwargs.copy()
        kwargs['code'] = code
        self._loadactionset('datastep')
        out = self.get_connection().retrieve('datastep.runcode', *args, **kwargs)
        try:
            return out['outputTables']['casTable'][0]
        except (KeyError, IndexError):
            pass
        raise SWATError(out.status)

#   def isin(self, values, casout=None):
#       raise NotImplementedError

#   def where(self, cond, other=np.nan, inplace=False, axis=None, level=None,
#             try_cast=False, raise_on_error=True):
#       raise NotImplementedError

#   def mask(self, cond, other=np.nan, inplace=False, axis=None, level=None,
#            try_cast=False, raise_on_error=True):
#       raise NotImplementedError

#   def query(self, **kwargs):
#       raise NotImplementedError

    # Binary operator functions

    # TODO: These could probably be implemented using the datastep action.
    #       The 'other' variable could be a CASTable or a DataFrame.
    #       For DataFrames, the data would be uploaded as a CAS table first.

#   def add(self, other, **kwargs):
#       raise NotImplementedError

#   def sub(self, other, **kwargs):
#       raise NotImplementedError

#   def mul(self, other, **kwargs):
#       raise NotImplementedError

#   def div(self, other, **kwargs):
#       raise NotImplementedError

#   def truediv(self, other, **kwargs):
#       raise NotImplementedError

#   def floordiv(self, other, **kwargs):
#       raise NotImplementedError

#   def mod(self, other, **kwargs):
#       raise NotImplementedError

#   def pow(self, other, **kwargs):
#       raise NotImplementedError

#   def radd(self, other, **kwargs):
#       raise NotImplementedError

#   def rsub(self, other, **kwargs):
#       raise NotImplementedError

#   def rmul(self, other, **kwargs):
#       raise NotImplementedError

#   def rdiv(self, other, **kwargs):
#       raise NotImplementedError

#   def rtruediv(self, other, **kwargs):
#       raise NotImplementedError

#   def rfloordiv(self, other, **kwargs):
#       raise NotImplementedError

#   def rmod(self, other, **kwargs):
#       raise NotImplementedError

#   def rpow(self, other, **kwargs):
#       raise NotImplementedError

    # TODO: Comparisons should return a new CASTable with a WHERE expression
    #       that implements the filtering.

#   def lt(self, other, **kwargs):
#       raise NotImplementedError

#   def gt(self, other, **kwargs):
#       raise NotImplementedError

#   def le(self, other, **kwargs):
#       raise NotImplementedError

#   def ge(self, other, **kwargs):
#       raise NotImplementedError

#   def ne(self, other, **kwargs):
#       raise NotImplementedError

#   def eq(self, other, **kwargs):
#       raise NotImplementedError

#   def combine(self, other, **kwargs):
#       raise NotImplementedError

#   def combineAdd(self, other):
#       raise NotImplementedError

#   def combine_first(self, other):
#       raise NotImplementedError

#   def combineMult(self, other):
#       raise NotImplementedError

    # Function application, GroupBy

#   def apply(self, func, **kwargs):
#       raise NotImplementedError

#   def applymap(self, func):
#       raise NotImplementedError

#   def groupby(self, *args):
#       raise NotImplementedError

    # Computations / Descriptive Stats

    # TODO: Operations that don't reduce the data down to one scalar per
    #       column, return a new CASTable object.

    def _summary(self, **kwargs):
        ''' Get summary DataFrame '''
        out = self._retrieve('simple.summary', **kwargs).get_tables('Summary')
        out = [x.reshape_bygroups(bygroup_columns='formatted', bygroup_as_index=True) for x in out]
        out = pd.concat(out)
        out = out.set_index('Column', append=self.has_groupby_vars())
        out = out.rename(columns=dict((k, k.lower()) for k in out.columns))
        out = out.rename(columns=dict(n='count'))
        out = out.stack().unstack('Column')
        out.columns.name = None
        return out

#   def abs(self):
#       raise NotImplementedError

#   def all(self, *args, **kwargs):
#       raise NotImplementedError

#   def any(self, *args, **kwargs):
#       raise NotImplementedError

#   def clip(self, *args, **kwargs):
#       raise NotImplementedError

#   def clip_lower(self, threshold, **kwargs):
#       raise NotImplementedError

#   def clip_upper(self, threshold, **kwargs):
#       raise NotImplementedError

    def corr(self, method=None, min_periods=None):
        ''' Compute pairwise correlation of columns '''
        # TODO: Need groupby support once simple.correlation adds it
        tbl = self.select_dtypes(include='numeric')
        out = tbl._retrieve('simple.correlation', simple=False)['Correlation']
        out.set_index('Variable', inplace=True)
        out.index.name = None
        return out

#   def corrwith(self, other, axis=None, drop=None):
#       raise NotImplementedError

    def count(self, axis=0, level=None, numeric_only=False):
        '''
        Return total number of non-missing values in each column

        Parameters
        ----------
        axis : int, optional
            Unsupported
        level : int or level name, optional
            Unsupported
        numeric_only : boolean, optional
            Include only numeric columns.

        Returns
        -------
        pandas.Series object

        '''
        tbl = self

        if numeric_only:
            tbl = tbl.select_dtypes(include='numeric')

        groups = self.get_groupby_vars()
        if groups:
            # TODO: Only supports numeric variables
            return self._summary().xs('count', level=len(groups))

        out = tbl._retrieve('simple.distinct')['Distinct']
        out.set_index('Column', inplace=True)
        return out['NMiss'].astype(np.int64).rsub(self._numrows)

#   def cov(self, min_periods=None):
#       raise NotImplementedError

#   def cummax(self, **kwargs):
#       raise NotImplementedError

#   def cummin(self, **kwargs):
#       raise NotImplementedError

#   def cumprod(self, **kwargs):
#       raise NotImplementedError

#   def cumsum(self, **kwargs):
#       raise NotImplementedError

    def _percentiles(self, percentiles=[25, 50, 75], format_labels=True):
        '''
        Return the requested percentile values

        Parameters
        ----------
        percentiles : list-of-ints, optional
            The percentile values (0-100) to compute

        Returns
        -------
        DataFrame

        '''
        self._loadactionset('percentile')

        if not isinstance(percentiles, items_types):
            percentiles = [percentiles]
        else:
            percentiles = list(percentiles)

        out = self._retrieve('percentile.percentile', values=percentiles)
        out = [x.reshape_bygroups(bygroup_columns='formatted', bygroup_as_index=True)
               for x in out.get_tables('Percentile')]
        out = pd.concat(out)

        if format_labels:
            out['Pctl'] = out['Pctl'].apply('{:,.0f}%'.format)
        else:
            out['Pctl'] = out['Pctl'].div(100)

        out = out.set_index(['Pctl', 'Variable'], append=self.has_groupby_vars())['Value']
        out = out.unstack()

        if len(out.index.names) > 1:
            out = out.set_index(pd.MultiIndex(levels=out.index.levels,
                                              labels=out.index.labels,
                                              names=out.index.names[:-1] + [None]))
        else:
            out.index.name = None

        out.columns.name = None

        return out

    def _topk_frequency(self, maxtie=0):
        ''' Return the top value by frequency '''
        out = self._retrieve('simple.topk', topk=1, bottomk=0,
                             maxtie=maxtie, order='freq').get_tables('Topk')
        out = [x.reshape_bygroups(bygroup_columns='formatted', bygroup_as_index=True) for x in out]
        out = pd.concat(out)
        out = out.set_index('Column', append=self.has_groupby_vars())
        out = out.drop('Rank', axis=1)

        if 'NumVar' in out.columns and 'CharVar' in out.columns:
            out['NumVar'].fillna(out['CharVar'], inplace=True)
            out.drop('CharVar', axis=1, inplace=True)
            out.rename(columns=dict(NumVar='top'), inplace=True)

        if 'NumVar' in out.columns:
            out.rename(columns=dict(NumVar='top'), inplace=True)
        elif 'CharVar' in out.columns:
            out.rename(columns=dict(CharVar='top'), inplace=True)

        out.rename(columns=dict(Score='freq'), inplace=True)
        out = out.stack().unstack('Column')
        out.columns.name = None

        return out

    def describe(self, percentiles=None, include=None, exclude=None, stats=None):
        '''
        Get descriptive statistics

        Parameters
        ----------
        percentiles : list-of-floats, optional
            The percentiles to include in the output.  The values should be
            in the interval [0, 1].  By default, `percentiles` is [0.25, 0.5, 0.75],
            returning the 25th, 50th, and 75th percentiles.

        include, exclude : list or 'all' or None (default), optional
            Specify the data types to include in the result.
            * If both are None, The result will include only numeric columns, or
              if no numeric columns are present, all columns are included.
            * A list of dtypes or strings.  To select all numerics, use the
              value 'number' or 'numeric'.  For all character columns, use
              the value 'character'.
            * If the value is 'all', all columns will be used.

        stats : list-of-strings or 'all' or None (default), optional
            The statistics to include in the output.  By default, numeric
            columns return 'count', 'std', 'min', 'pct', 'max',
            where 'pct' is the collection of percentiles specified in the
            `percentiles` argument.  Character statistics include 'count',
            'unique', 'top', and 'freq'.  In addition, the following can be
            specified, 'nmiss', 'sum', 'stderr', 'var', 'uss', 'cv', 'tvalue',
            and 'probt'.  If 'all' is specified, all relevant statistics
            will be returned.

        Returns
        -------
        DataFrame

        '''
        numrows = self._numrows

        # Auto-specify all numeric or all character
        if include is None and exclude is None:
            include = ['number']
            tbl = self.select_dtypes(include=['number'])
            if not tbl.get_param('varlist', []):
                tbl = self.select_dtypes(include=['character'])

        # include/exclude was specified by the user
        else:
            tbl = self.select_dtypes(include=include, exclude=exclude)

        # Get percentiles
        if percentiles is not None:
            if not isinstance(percentiles, items_types):
                percentiles = [percentiles]
            else:
                percentiles = list(percentiles)
            for i, pct in enumerate(percentiles):
                percentiles[i] = max(min(pct * 100, 100), 0)

        if not percentiles:
            percentiles = [25, 50, 75]

        if 50 not in percentiles:
            percentiles = percentiles + [50]

        percentiles = _get_unique(sorted(percentiles))

        columns = tbl.columns
        dtypes = tbl.dtypes
        char_types = set(['char', 'varchar', 'binary', 'varbinary'])

        # See if we need to do numeric summarization
        has_numeric = set(dtypes).difference(char_types) and True or False
        has_character = set(dtypes).intersection(char_types) and True or False

        # Get top value and frequency
        topk_freq = None
        if stats is None or stats == 'all' or 'freq' in stats or 'top' in stats:
            topk_freq = tbl._topk_frequency()

        # Get unique value counts
        topk_val = None
        if stats is None or stats == 'all' or 'unique' in stats \
                or 'max' in stats or 'min' in stats:
            topk_val = tbl._topk_values(leave_index=True)

        def _expand_items(into, key, items):
            ''' Expand a single element with a collection '''
            if not isinstance(items, items_types):
                items = [items]
            out = []
            for elem in into:
                if elem == key:
                    for item in items:
                        out.append(item)
                else:
                    out.append(elem)
            return out

        if has_numeric:
            pct_labels = ['%d%%' % x for x in percentiles]

            if stats is None:
                labels = ['count', 'mean', 'std', 'min', 'pct', 'max']

                if has_character:
                    labels = _expand_items(labels, 'count',
                                           ['count', 'unique', 'top', 'freq'])

            elif stats == 'all':
                labels = ['count', 'unique', 'mean', 'std', 'min', 'pct'] + \
                         ['max', 'nmiss', 'sum', 'stderr', 'var', 'uss'] + \
                         ['cv', 'tvalue', 'probt']

                if has_character:
                    labels = _expand_items(labels, 'unique',
                                           ['unique', 'top', 'freq'])

            else:
                labels = stats

            if 'pct' in labels:
                labels = _expand_items(labels, 'pct', pct_labels)

            # Create table with only numeric columns
            numtbl = tbl.select_dtypes(include=['numeric'])

            # Get percentiles
            pct = numtbl._percentiles(percentiles=percentiles)

            # Get remaining summary values
            summ = numtbl._summary()
            if len(summ.index.names) > 1:
                summ.drop(['min', 'max'], level=-1, inplace=True)
            else:
                summ.drop(['min', 'max'], inplace=True)

            out = pd.concat(x for x in [topk_val, pct, summ, topk_freq]
                            if x is not None)

        else:
            if stats is None:
                labels = ['count', 'unique', 'top', 'freq']
            elif stats == 'all':
                labels = ['count', 'unique', 'top', 'freq', 'min', 'max']
            else:
                labels = stats
            out = pd.concat(x for x in [topk_freq, topk_val] if x is not None)

        groups = self.get_groupby_vars()
        idx = tuple([slice(None) for x in groups] + [labels])
        columns = [x for x in columns if x not in groups]

        def fillna(dframe, label, value):
            ''' Fill values at label with nan '''
            try:
                dframe.loc[label].fillna(value, axis='index', inplace=True)
            except KeyError:
                dframe.loc[label] = value

        if not groups:
            if 'nmiss' in labels:
                fillna(out, 'nmiss', 0)
            if 'count' in labels:
                fillna(out, 'count', numrows)
            return out.loc[idx[0], columns]

        # TODO: Still need counts for character columns when by grouped

        out.sort_index(inplace=True)

        out = out.loc[idx, columns]

        categories = ['count', 'unique', 'top', 'freq', 'mean', 'std', 'min'] + \
                     ['%d%%' % x for x in range(101)] + \
                     ['max', 'nmiss', 'sum', 'stderr', 'var', 'uss'] + \
                     ['cv', 'tvalue', 'probt']

        # This is done so that the row labels will come out in category-sorted order.
        tmpname = str(uuid.uuid4())
        out.index.names = groups + [tmpname]
        out.reset_index(inplace=True)
        out[tmpname] = out[tmpname].astype('category', categories=categories,
                                           ordered=True)
        out.sort_values(groups + [tmpname], inplace=True)
        out.set_index(groups + [tmpname], inplace=True)
        out.index.names = groups + [None]

        return out

#   def diff(self, periods=1, axis=0):
#       raise NotImplementedError

#   def eval(self, expr, **kwargs):
#       raise NotImplementedError

#   def kurt(self, *args, **kwargs):
#       raise NotImplementedError

#   def mad(self, *args, **kwargs):
#       raise NotImplementedError

    def _get_summary_stat(self, name):
        '''
        Run simple.summary and get the given statistic

        Parameters
        ----------
        name : string
            The name of the simple.summary column.

        Returns
        -------
        DataFrame
            for multi-index output on CASTable
        Series
            for single index output on CASTable, or multi-index output on CASColumn
        scalar
            for single index output on CASColumn

        '''
        name = name.lower()
        out = self._summary()
        if len(out.index.names) > 1:
            return out.xs(name, level=-1)
        out = out.xs(name)
        out.index.name = None
        out.name = None
        return out

    def _topk_values(self, stats=['unique', 'min', 'max'], axis=None, skipna=True,
                     level=None, numeric_only=False, leave_index=False, **kwargs):
        '''
        Compute min / max / unique value(s)

        Parameters
        ----------
        stats : string or list-of-strings, optional
            'unique', 'min', 'max' or a list of any combination.

        Returns
        -------
        DataFrame

        '''
        from ..dataframe import reshape_bygroups

        tbl = self

        if numeric_only:
            tbl = tbl.select_dtypes(include='numeric')

        if not isinstance(stats, items_types):
            stats = [stats]
        else:
            stats = list(stats)

        out = tbl.simple.topk(order='value', includemissing=not skipna,
                              topk=1, bottomk=1, **kwargs)

        groups = tbl.get_groupby_vars()
        groupset = set(groups)
        columns = [x for x in tbl.columns if x not in groupset]

        # Minimum / Maximum
        minmax = None
        if 'min' in stats or 'max' in stats:
            minmax = [x.reshape_bygroups(bygroup_columns='formatted', bygroup_as_index=False)
                      for x in out.get_tables('Topk')]
            minmax = pd.concat(minmax) 
            #minmax = out['Topk']
            minmax.loc[:, 'stat'] = ['max', 'min'] * int(len(minmax) / 2)
            if 'NumVar' in minmax.columns and 'CharVar' in minmax.columns:
                minmax['NumVar'].fillna(minmax['CharVar'], inplace=True)
                minmax.rename(columns=dict(NumVar='value', Column='column'),
                              inplace=True)
            elif 'NumVar' in minmax.columns:
                minmax.rename(columns=dict(NumVar='value', Column='column'),
                              inplace=True)
            else:
                minmax.rename(columns=dict(CharVar='value', Column='column'),
                              inplace=True)
            minmax = minmax.loc[:, groups + ['stat', 'column', 'value']]
            if skipna:
                minmax.dropna(inplace=True)
            if 'min' not in stats:
                minmax = minmax.set_index('stat').drop('min').reset_index()
            if 'max' not in stats:
                minmax = minmax.set_index('stat').drop('max').reset_index()
            minmax.set_index(groups + ['stat', 'column'], inplace=True)
            if groups:
                minmax.drop(groups, level=-1, inplace=True)
            minmax = minmax.unstack()
            minmax.index.name = None
            minmax.columns.names = [None] * len(minmax.columns.names)
            minmax.columns = minmax.columns.droplevel()
            minmax = minmax.loc[:, columns]

        # Unique
        unique = None
        if 'unique' in stats:
            unique = [x.reshape_bygroups(bygroup_columns='formatted', bygroup_as_index=False)
                      for x in out.get_tables('TopkMisc')]
            unique = pd.concat(unique) 
            #unique = out['TopkMisc']
            unique.loc[:, 'unique'] = 'unique'
            unique.rename(columns=dict(N='value', Column='column'), inplace=True)
            unique = unique.loc[:, groups + ['unique', 'column', 'value']]
            if skipna:
                unique.dropna(inplace=True)
            unique.set_index(groups + ['unique', 'column'], inplace=True)
            if groups:
                unique.drop(groups, level=-1, inplace=True)
            unique = unique.unstack()
            unique.index.name = None
            unique.columns.names = [None] * len(unique.columns.names)
            unique.columns = unique.columns.droplevel()
            unique = unique.loc[:, columns]

        out = pd.concat(x for x in [unique, minmax] if x is not None)
        out = out.sort_index(ascending=([True] * len(groups)) + [False])

        if len(stats) > 1 or leave_index:
            return out

        if len(out.index.names) > 1:
            return out.xs(stats[0], level=-1)

        return out.loc[stats[0]]

    def max(self, axis=None, skipna=True, level=None, numeric_only=False, **kwargs):
        '''
        Return the maximum value of each column

        Parameters
        ----------
        axis : int, optional
            Unsupported
        skipna : boolean, optional
            Exclude missing values from the computation.
        level : int or string, optional
            Unsupported
        numeric_only : boolean, optional
            Include only numeric columns in the computation.

        Returns
        -------
        Series
            if no by groups are specified
        Dataframe
            if by groups are specified

        '''
        return self._topk_values('max', axis=axis, skipna=skipna, level=level,
                                 numeric_only=numeric_only, **kwargs)

    def mean(self, axis=None, skipna=True, level=None, numeric_only=False):
        '''
        Return the mean value of each column

        Parameters
        ----------
        axis : int, optional
            Unsupported
        skipna : boolean, optional
            Unsupported
        level : int or string, optional
            Unsupported
        numeric_only : boolean, optional
            Include only numeric columns in the computation.

        Returns
        -------
        Series
            if no by groups are specified
        Dataframe
            if by groups are specified

        '''
        return self._get_summary_stat('mean')

    def median(self, axis=None, skipna=None, level=None, numeric_only=None, **kwargs):
        '''
        Return the median value of each numeric column

        Parameters
        ----------
        axis : int, optional
            Unsupported
        skipna : boolean, optional
            Exclude missing values from the computation.
        level : int or string, optional
            Unsupported
        numeric_only : boolean, optional
            Include only numeric columns in the computation.

        Returns
        -------
        Series
            if no by groups are specified
        Dataframe
            if by groups are specified

        '''
        return self.quantile(0.5, axis=axis, interpolation='nearest')

    def min(self, axis=None, skipna=True, level=None, numeric_only=False, **kwargs):
        '''
        Return the minimum value of each column

        Parameters
        ----------
        axis : int, optional
            Unsupported
        skipna : boolean, optional
            Exclude missing values from the computation.
        level : int or string, optional
            Unsupported
        numeric_only : boolean, optional
            Include only numeric columns in the computation.

        Returns
        -------
        Series
            if no by groups are specified
        Dataframe
            if by groups are specified

        '''
        return self._topk_values('min', axis=axis, skipna=skipna, level=level,
                                 numeric_only=numeric_only, **kwargs)

    def nlargest(self, n, columns, keep='first'):
        ''' Return the `n` largest values ordered by `columns` '''
        if not isinstance(columns, items_types):
            columns = [columns]
        columns = [dict(name=x, order='DESCENDING', formatted='RAW') for x in columns]
        col = self.copy(deep=True)
        col._action_params = {}
        return col._fetch(from_=1, to=n, sortby=columns)

    def nsmallest(self, n, columns, keep='first'):
        ''' Return the `n` smallest values ordered by `columns` '''
        if not isinstance(columns, items_types):
            columns = [columns]
        columns = [dict(name=x, order='ASCENDING', formatted='RAW') for x in columns]
        col = self.copy(deep=True)
        col._action_params = {}
        return col._fetch(from_=1, to=n, sortby=columns)

    def mode(self, axis=0, numeric_only=False, max_tie=100):
        '''
        Return the mode of each column

        Parameters
        ----------
        axis : int, optional
            Unsupported
        numeric_only : boolean, optional
            Should only numeric columns be used?

        Series
            if no by groups are specified
        Dataframe
            if by groups are specified

        '''
        # TODO: If a column has all unique values, it should just be set to NaN.

        tbl = self

        if numeric_only:
            tbl = tbl.select_dtypes(include='numeric')

        columns = list(tbl.columns)

        out = tbl._retrieve('simple.topk', order='freq', topk=1,
                            bottomk=0, maxtie=max_tie)['Topk']

        out['Rank'] = out['Rank'] - 1

        groups = tbl.get_groupby_vars()
        if groups:
            out = out.drop(out.columns[1:len(groups)*2:2], axis=1)
            items = []
            for key, item in out.groupby(groups):
                if not isinstance(key, items_types):
                    key = [key]
                charout = numout = None
                if 'CharVar' in out.columns:
                    charout = item.pivot(columns='Column', values='CharVar',
                                         index='Rank').replace([None, ''], np.nan)
                if 'NumVar' in out.columns:
                    numout = item.pivot(columns='Column', values='NumVar', index='Rank')
                if numout is not None and charout is not None:
                    item = numout.fillna(charout)
                elif numout is not None:
                    item = numout
                elif charout is not None:
                    item = charout
                for name, value in zip(groups, key):
                    item[name] = value

                item = pd.concat([item[col].sort_values(ascending=True,
                                  inplace=False).reset_index(drop=True)
                                  for col in columns], axis=1)

                item = item.set_index(groups, append=True)
                item = item.reorder_levels(groups + [None])
                item.index.names = list(item.index.names[:-1]) + [None]

                items.append(item)

            out = pd.concat(items)

        else:
            charout = numout = None
            if 'CharVar' in out.columns:
                charout = out.pivot(columns='Column', values='CharVar',
                                    index='Rank').replace([None, ''], np.nan)
            if 'NumVar' in out.columns:
                numout = out.pivot(columns='Column', values='NumVar', index='Rank')
            if numout is not None and charout is not None:
                out = numout.fillna(charout)
            elif numout is not None:
                out = numout
            elif charout is not None:
                out = charout

            out = pd.concat([out[col].sort_values(ascending=True).reset_index(drop=True)
                             for col in columns], axis=1)

        out.columns.name = None
        out.index.name = None

        return out

#   def pct_change(self, *args, **kwargs):
#       raise NotImplementedError

#   def prod(self, *args, **kwargs):
#       raise NotImplementedError

    def quantile(self, q=0.5, axis=0, numeric_only=True, interpolation='nearest'):
        '''
        Return values at the given quantile

        Parameters
        ----------
        q : float, optional
            The quantiles to compute (0 <= q <= 1)
        axis : int, optional
            Unsupported
        interpolation : string, optional
            Only 'nearest' is supported

        Returns
        -------
        Series
            if no by groups are specified, or only a single quantile is requested
        Dataframe
            if by groups are specified

        '''
        tbl = self

        if numeric_only:
            tbl = tbl.select_dtypes(include='numeric')

        single_quantile = False
        if not isinstance(q, items_types):
            q = [q]
            single_quantile = True

        groups = tbl.get_groupby_vars()

        columns = [x for x in tbl.columns if x not in groups]

        out = tbl._percentiles(percentiles=[x * 100 for x in q],
                               format_labels=False)[columns]

        if single_quantile:
            out = out.reset_index(level=-1, drop=True)
            if not groups:
                out = out.stack().reset_index(level=0, drop=True)

        return out

#   def rank(self, *args, **kwargs):
#       raise NotImplementedError

#   def sem(self, *args, **kwargs):
#       raise NotImplementedError

#   def skew(self, *args, **kwargs):
#       raise NotImplementedError

    def sum(self, axis=None, skipna=None, level=None, numeric_only=True):
        ''' Return the sum of each column '''
        # TODO: Need character variables (??)
        return self._get_summary_stat('sum')

    def std(self, axis=None, skipna=None, level=None, ddof=1, numeric_only=True):
        ''' Return the standard deviation of each column '''
        return self._get_summary_stat('std')

    def var(self, axis=None, skipna=None, level=None, ddof=1, numeric_only=True):
        ''' Return the variance of each column '''
        return self._get_summary_stat('var')

    # Not DataFrame methods, but they are available statistics.

    def nmiss(self):
        ''' Return the number of missing values in each column '''
        return self._get_summary_stat('nmiss').astype(np.int64)

    def stderr(self):
        ''' Return the standard error of each column '''
        return self._get_summary_stat('stderr')

    def uss(self):
        ''' Return the uncorrected sum of squares of each column '''
        return self._get_summary_stat('uss')

    def css(self):
        ''' Return the corrected sum of squares of each column '''
        return self._get_summary_stat('css')

    def cv(self):
        ''' Return the coefficient of variation of each column '''
        return self._get_summary_stat('cv')

    def tvalue(self):
        ''' Return the value of T-statistics for hypothesis testing for each column '''
        return self._get_summary_stat('tvalue')

    def probt(self):
        ''' Return the p-value of the T-statistics for each column '''
        return self._get_summary_stat('probt')

    # Reindexing / Selection / Label manipulation

#   def add_prefix(self, prefix):
#       raise NotImplementedError

#   def add_suffix(self, suffix):
#       raise NotImplementedError

#   def align(self, *args, **kwargs):
#       raise NotImplementedError

#   def drop(self, *args, **kwargs):
#       raise NotImplementedError

#   def drop_duplicates(self, *args, **kwargs):
#       raise NotImplementedError

#   def duplicated(self, *args, **kwargs):
#       raise NotImplementedError

#   def equals(self, *args, **kwargs):
#       raise NotImplementedError

#   def filter(self, *args, **kwargs):
#       raise NotImplementedError

#   def first(self, *args, **kwargs):
#       raise NotImplementedError

#   def idxmax(self, *args, **kwargs):
#       raise NotImplementedError

#   def idxmin(self, *args, **kwargs):
#       raise NotImplementedError

#   def last(self, *args, **kwargs):
#       raise NotImplementedError

#   def reindex(self, *args, **kwargs):
#       raise NotImplementedError

#   def reindex_axis(self, *args, **kwargs):
#       raise NotImplementedError

#   def reindex_like(self, *args, **kwargs):
#       raise NotImplementedError

#   def rename(self, *args, **kwargs):
#       raise NotImplementedError

#   def reset_index(self, *args, **kwargs):
#       raise NotImplementedError

#   def sample(self, *args, **kwargs):
#       raise NotImplementedError

#   def select(self, *args, **kwargs):
#       raise NotImplementedError

#   def take(self, *args, **kwargs):
#       raise NotImplementedError

#   def truncate(self, *args, **kwargs):
#       raise NotImplementedError

    # Missing data handling

#   def dropna(self, *args, **kwargs):
#       raise NotImplementedError

#   def fillna(self, *args, **kwargs):
#       raise NotImplementedError

#   def replace(self, *args, **kwargs):
#       raise NotImplementedError

    # Reshaping, sorting, transposing

#   def pivot(self, *args, **kwargs):
#       raise NotImplementedError

#   def reorder_levels(self, *args, **kwargs):
#       raise NotImplementedError

    def sort_values(self, by, axis=0, ascending=True, inplace=False,
                    kind='quicksort', na_position='last'):
        '''
        Sort data in CAS table

        Parameters
        ----------
        by : string or list-of-strings
            The name or names of columns to sort by.
        axis : int, optional
            Unsupported
        ascending : boolean or list-of-booleans, optional
            Sort ascending or descending.  Specify a list of booleanss
            if sort orders are not all one type.
        inplace : boolean, optional
            If True, the sort order is embedded into the CASTable
            instance.  If False, a new CASTable is returned with the
            sort parameters embedded.
        kind : string, optional
            Unsupported
        na_position : string, optional
            Unsupported

        Returns
        -------
        None
            If inplace = True
        CASTable
            If inplace = False

        '''
        if inplace:
            out = self
        else:
            out = copy.deepcopy(self)
        if not isinstance(by, items_types):
            by = [by]
        if not isinstance(ascending, items_types):
            ascending = [ascending] * len(by)
        for col, asc in zip(by, ascending):
            fetch = out.get_action_params('table.fetch', {})
            fetch.setdefault('sortby', []).append(
                dict(name=col, order=asc and 'ASCENDING' or 'DESCENDING',
                     formatted='RAW'))
            out.set_action_params('table.fetch', **fetch)
        if inplace:
            return
        return out

    sort = sort_values

#   def sort_index(self, *args, **kwargs):
#       raise NotImplementedError

#   def sortlevel(self, *args, **kwargs):
#       raise NotImplementedError

#   def swaplevel(self, *args, **kwargs):
#       raise NotImplementedError

#   def stack(self, *args, **kwargs):
#       raise NotImplementedError

#   def unstack(self, *args, **kwargs):
#       raise NotImplementedError

#   @getattr_safe_property
#   def T(self):
#       return self.transpose()

    def to_view(self, *args, **kwargs):
        '''
        Create a view using the current CASTable parameters

        The parameters to this method are the same as the table.view
        action.  `self` will automatically be added as the `tables`
        parameter.

        See Also
        --------
        table.view action

        Returns
        -------
        CASTable object

        '''
        kwargs = kwargs.copy()
        kwargs['tables'] = [self]
        if not args and 'name' not in kwargs:
            kwargs['name'] = _gen_table_name()
        out = self._retrieve('table.view', *args, **kwargs)
        if 'caslib' in out and 'viewName' in out:
            conn = self.get_connection()
            return conn.CASTable(out['viewName'], caslib=out['caslib'])
        raise SWATError('No output table was returned')

#   def to_panel(self, *args, **kwargs):
#       raise NotImplementedError

#   def transpose(self, *args, **kwargs):
#       raise NotImplementedError

    # Combining / joining / merging

#   def append(self, other, **kwargs):
#       raise NotImplementedError

#   def assign(self, **kwargs):
#       raise NotImplementedError

#   def join(self, other, **kwargs):
#       raise NotImplementedError

#   def merge(self, right, **kwargs):
#       raise NotImplementedError

#   def update(self, other, **kwargs):
#       raise NotImplementedError

    # Time series-related

#   def asfreq(self, freq, **kwargs):
#       raise NotImplementedError

#   def shift(self, **kwargs):
#       raise NotImplementedError

#   def first_valid_index(self):
#       raise NotImplementedError

#   def last_valid_index(self):
#       raise NotImplementedError

#   def resample(self, rule, **kwargs):
#       raise NotImplementedError

#   def to_period(self, **kwargs):
#       raise NotImplementedError

#   def to_timestamp(self, **kwargs):
#       raise NotImplementedError

#   def tz_convert(self, tz, **kwargs):
#       raise NotImplementedError

#   def tz_localize(self, *args, **kwargs):
#       raise NotImplementedError

    # Plotting

    def _fetch(self, grouped=False, **kwargs):
        ''' Return the fetched DataFrame given the fetch parameters '''
        kwargs = kwargs.copy()
        if 'to' not in kwargs:
            kwargs['to'] = get_option('cas.dataset.max_rows_fetched')
        out = pd.concat(list(self._retrieve('table.fetch', noindex=True,
                                            sastypes=False, **kwargs).values()))
        groups = self.get_groupby_vars()
        if grouped and groups:
            return out.groupby(groups)
        return out

    def boxplot(self, *args, **kwargs):
        '''
        Make a boxplot from the table data

        See Also: pandas.DataFrame.boxplot (for arguments)

        '''
        return self._fetch(grouped=True).boxplot(*args, **kwargs)

    def hist(self, *args, **kwargs):
        '''
        Make a histogram from the table data

        See Also: pandas.DataFrame.hist (for arguments)

        '''
        return self._fetch(grouped=True).hist(*args, **kwargs)

    @getattr_safe_property
    def plot(self):
        ''' Table plotting accessor and method '''
        return self._plot

    # Serialization / IO / Conversion

    @classmethod
    def from_csv(cls, connection, path, header=0, sep=',', index_col=0, parse_dates=True,
                 tupleize_cols=False, infer_datetime_format=False, **kwargs):
        ''' Create a CASTable from a CSV file '''
        return connection.read_csv(path, header=header, sep=sep, index_col=index_col,
                                   parse_dates=parse_dates, tupleize_cols=tupleize_cols,
                                   infer_datetime_format=infer_datetime_format,
                                   **kwargs)

    @classmethod
    def _from_any(cls, name, connection, data, *args, **kwargs):
        ''' Upload data from various sources '''
        from swat.cas.datamsghandlers import PandasDataFrame
        table, kwargs = connection._get_table_args(*args, **kwargs)
        dmh = PandasDataFrame(getattr(pd.DataFrame, 'from_' + name)(data,
                                                                    *args,
                                                                    **kwargs))
        table.update(dmh.args.addtable)
        return connection.retrieve('table.addtable', **table)['casTable']

    @classmethod
    def from_dict(cls, connection, data, *args, **kwargs):
        ''' Create a CASTable from a dictionary '''
        return cls._from_any('dict', connection, data, *args, **kwargs)

    @classmethod
    def from_items(cls, connection, items, *args, **kwargs):
        ''' Create a CASTable from a (key, value) pairs '''
        return cls._from_any('items', connection, items, *args, **kwargs)

    @classmethod
    def from_records(cls, connection, data, *args, **kwargs):
        ''' Create a CASTable from records '''
        return cls._from_any('records', connection, data, *args, **kwargs)

    def info(self, verbose=None, buf=sys.stdout, max_cols=None,
             memory_usage=None, null_counts=None):
        ''' Concise summary of a CASTable '''
        buf.write(u'%s\n' % self)

        nrows, ncols = self.shape

        counts = self._retrieve('simple.distinct')['Distinct']
        counts.set_index('Column', inplace=True)
        counts['N'] = (nrows - counts['NMiss']).astype('i8')
        counts['Miss'] = counts['NMiss'] > 0
        counts = counts[['N', 'Miss']]

        colinfo = self._columninfo
        colinfo.set_index('Column', inplace=True)
        colinfo = colinfo[['Type']]
        if null_counts is None or null_counts is True:
            colinfo = colinfo.join(counts)[['N', 'Miss', 'Type']]
        else:
            colinfo = colinfo.join(counts)[['N', 'Type']]
        colinfo.index.name = None

        if verbose is None or verbose is True:
            buf.write(u'Data columns (total %s columns):\n' % ncols)
            if max_cols is not None:
                buf.write(u'%s\n' % colinfo.iloc[:max_cols])
                if max_cols < ncols:
                    buf.write(u'...\n')
            else:
                buf.write(u'%s\n' % colinfo)
        else:
            buf.write(u'Columns: %s entries, %s to %s\n' %
                      (ncols, colinfo.index[0], colinfo.index[-1]))

        buf.write(u'dtypes: %s\n' % ', '.join([u'%s(%s)' % (name, dtype)
                  for name, dtype in sorted(
                      colinfo['Type'].value_counts().to_dict().items())]))

        if memory_usage is None or memory_usage is True:
            details = self._retrieve('table.tabledetails')['TableDetails']
            details = details[['DataSize', 'VardataSize', 'AllocatedMemory']].iloc[0]
            buf.write(u'data size: %s\n' % details['DataSize'])
            buf.write(u'vardata size: %s\n' % details['VardataSize'])
            buf.write(u'memory usage: %s\n' % details['AllocatedMemory'])

    def to_frame(self, *args, **kwargs):
        ''' Retrieve entire table as a DataFrame '''
        return pd.concat(list(self._retrieve('table.fetch', sastypes=False,
                                            to=MAX_INT64_INDEX,
                                            noindex=True).values()))

    def _to_any(self, method, *args, **kwargs):
        ''' Generic converter to various output types '''
        standard_dataframe = kwargs.pop('standard_dataframe', False)
        dframe = pd.concat(list(self._retrieve('table.fetch', sastypes=False,
                                                to=get_option('cas.dataset.max_rows_fetched'),
                                                noindex=True).values()))
        if standard_dataframe:
            dframe = pd.DataFrame(dframe)
        return getattr(dframe, 'to_' + method)(*args, **kwargs)

    def to_xarray(self, *args, **kwargs):
        '''
        Return an xarray from the CASTable

        See Also: pandas.DataFrame.to_xarray (for arguments)

        '''
        return self._to_any('xarray', standard_dataframe=True, *args, **kwargs)

    def to_pickle(self, *args, **kwargs):
        '''
        Pickle (serialize) table data

        See Also: pandas.DataFrame.to_pickle (for arguments)

        '''
        return self._to_any('pickle', standard_dataframe=True, *args, **kwargs)

    def to_csv(self, *args, **kwargs):
        '''
        Write table data to comma separated values (CSV)

        See Also: pandas.DataFrame.to_csv (for arguments)

        '''
        return self._to_any('csv', *args, **kwargs)

    def to_hdf(self, *args, **kwargs):
        '''
        Write table data to HDF

        See Also: pandas.DataFrame.to_hdf (for arguments)

        '''
        return self._to_any('hdf', standard_dataframe=True, *args, **kwargs)

    def to_sql(self, *args, **kwargs):
        '''
        Write table records to SQL database

        See Also: pandas.DataFrame.to_sql (for arguments)

        '''
        return self._to_any('sql', *args, **kwargs)

    def to_dict(self, *args, **kwargs):
        '''
        Convert table data to dictionary

        See Also: pandas.DataFrame.to_dict (for arguments)

        '''
        return self._to_any('dict', *args, **kwargs)

    def to_excel(self, *args, **kwargs):
        '''
        Write table data to an Excel spreadsheet

        See Also: pandas.DataFrame.to_excel (for arguments)

        '''
        return self._to_any('excel', *args, **kwargs)

    def to_json(self, *args, **kwargs):
        '''
        Convert the table data to a JSON string

        See Also: pandas.DataFrame.to_json (for arguments)

        '''
        return self._to_any('json', *args, **kwargs)

    def to_html(self, *args, **kwargs):
        '''
        Render the table data to an HTML table

        See Also: pandas.DataFrame.to_html (for arguments)

        '''
        return self._to_any('html', *args, **kwargs)

    def to_latex(self, *args, **kwargs):
        '''
        Render the table data to a LaTeX tabular environment

        See Also: pandas.DataFrame.to_latex (for arguments)

        '''
        return self._to_any('latex', *args, **kwargs)

    def to_stata(self, *args, **kwargs):
        '''
        Write table data to Stata file

        See Also: pandas.DataFrame.to_stata (for arguments)

        '''
        return self._to_any('stata', *args, **kwargs)

    def to_msgpack(self, *args, **kwargs):
        '''
        Write table data to msgpack object

        See Also: pandas.DataFrame.to_msgpack (for arguments)

        '''
        return self._to_any('msgpack', standard_dataframe=True, *args, **kwargs)

    def to_gbq(self, *args, **kwargs):
        '''
        Write table data to a Google BigQuery table

        See Also: pandas.DataFrame.to_gbq (for arguments)

        '''
        return self._to_any('gbq', *args, **kwargs)

    def to_records(self, *args, **kwargs):
        '''
        Convert table data to record array

        See Also: pandas.DataFrame.to_records (for arguments)

        '''
        return self._to_any('records', *args, **kwargs)

    def to_sparse(self, *args, **kwargs):
        '''
        Convert table data to SparseDataFrame

        See Also: pandas.DataFrame.to_sparse (for arguments)

        '''
        return self._to_any('sparse', *args, **kwargs)

    def to_dense(self, *args, **kwargs):
        '''
        Return dense representation of table data

        See Also: pandas.DataFrame.to_dense (for arguments)

        '''
        return self._to_any('dense', *args, **kwargs)

    def to_string(self, *args, **kwargs):
        '''
        Render the table to a console-friendly tabular output

        See Also: pandas.DataFrame.to_string (for arguments)

        '''
        return self._to_any('string', *args, **kwargs)

    def to_clipboard(self, *args, **kwargs):
        '''
        Write the table data to the clipboard

        See Also: pandas.DataFrame.to_clipboard (for arguments)

        '''
        return self._to_any('clipboard', *args, **kwargs)

    # Fancy indexing

    def __setitem__(self, key, value):
        '''
        Create a new computed column

        Parameters
        ----------
        key : string
            The name of the column
        value : CASColumn or any
            The value of the column

        '''
        compvars = [key]
        comppgm = []

        if isinstance(value, CASColumn):
            cexpr, cvars, cpgm = value._to_expression()
            comppgm.append(cpgm)
            comppgm.append('%s = %s; ' % (key, cexpr))

        elif isinstance(value, text_types) or isinstance(value, binary_types):
            comppgm.append('%s = "%s"; ' % (key, _escape_string(value)))

        else:
            comppgm.append('%s = %s; ' % (key, value))

        self.append_computed_columns(compvars, comppgm)
        self.append_varlist(key)

    def __getitem__(self, key):
        '''
        Retrieve a slice of a CASTable / CASColumn

        Returns
        -------
        CASTable for the following:
            tbl[collist]
            tbl[rowslice|int]
            tbl[rowslice|int|rowlist, colslice|int|colname|collist]

        CASColumn for the following:
            tbl[colname] => CASColumn

        Scalar for the following:
            col[int]

        '''
        is_column = isinstance(self, CASColumn)

        # tbl[colname]
        if not(is_column) and (isinstance(key, text_types) or
                               isinstance(key, binary_types)):
            columns = set([x.lower() for x in list(self.columns)])
            if key.lower() not in columns:
                raise KeyError(key)
            return self._to_column(key)

        # tbl[[colnames|colindexes]]
        if not(is_column) and (isinstance(key, list) or isinstance(key, pd.Index)):
            out = copy.deepcopy(self)
            columns = list(out.columns)
            colset = set([x.lower() for x in columns])
            compvars = []
            comppgm = []
            varlist = []
            for k in key:
                if isinstance(k, int_types):
                    k = columns[k]
                if k.lower() not in colset:
                    compvars.append(k)
                    comppgm.append('%s = .; ' % _nlit(k))
                varlist.append(k)
            out.set_param('varlist', varlist)
            if compvars:
                out.append_computed_columns(compvars, comppgm)
            return out

        # tbl[CASColumn]
        if isinstance(key, CASColumn):
            out = copy.deepcopy(self)
            expr, ecompvars, ecomppgm = key._to_expression()

            out.append_where(expr)

            if ecompvars:
                out.append_compvars(ecompvars)

            if ecomppgm:
                out.append_comppgm(ecomppgm)

            if out.get_param('varlist', None) is None:
                out.set_param('varlist', list(self.columns))

            return out

        # tbl[rowslice]
        if isinstance(key, slice):
            return self.ix[key]

        # col[row]
        if is_column and isinstance(key, int_types):
            return self.ix[key]

        # Everything else
        raise KeyError(key)

    def groupby(self, by, axis=0, level=None, as_index=True, sort=True,
                group_keys=True, squeeze=False, **kwargs):
        ''' Specify grouping variables for the table '''
        return CASTableGroupBy(self, by, axis=axis, level=level, as_index=as_index,
                               sort=sort, group_keys=group_keys, squeeze=squeeze,
                               **kwargs)

    def query(self, expr, inplace=False, engine='cas', **kwargs):
        '''
        Query the table with a boolean expression

        Parameters
        ----------
        expr : string
            The query string to evaluate.  The expression must be a valid
            CAS expression.
        inplace : boolean, optional
            Whether the CASTable should be modified in place, or a copy
            should be returned.
        **kwargs : dict, optional
            Unsupported.

        Returns
        -------
        None
            if inplace=True
        CASTable object
            if inplace=False

        '''
        if engine != 'cas':
            raise SWATError('Only CAS queries are supported at this time.')
        tbl = self
        if not inplace:
            tbl = tbl.copy(deep=True)
        tbl.append_where(expr)
        return tbl

#   def where(self, cond, other=None, inplace=False, axis=None, level=None,
#             try_cast=False, raise_on_error=True):
#       if not isinstance(cond, CASColumn):
#           raise TypeError('Only CASTable conditions are supported')

#       out = self
#       if not inplace:
#           out = copy.deepcopy(out)

#       expr, ecompvars, ecomppgm = cond._to_expression()

#       out.append_where(expr)

#       if ecompvars:
#           out.append_compvars(ecompvars)

#       if ecomppgm:
#           out.append_comppgm(ecomppgm)

#       if out.get_param('varlist', None) is None:
#           out.set_param('varlist', list(self.columns))

#       if not inplace:
#           return out

    def get_groupby_vars(self):
        ''' Return a list of groupby variables '''
        groups = []
        if self.has_groupby_vars():
            groups = self.get_param('groupby')
            if not isinstance(groups, items_types):
                groups = [groups]
            groups = [x for x in groups if x]
        return groups

    def has_groupby_vars(self):
        ''' Does the table have By group variables configured? '''
        return self.has_param('groupby') and self.get_param('groupby')


class CharacterColumnMethods(object):
    ''' CASColumn string methods '''

    def __init__(self, column):
        self._column = column
        if column._is_numeric():
            raise TypeError('string methods are not usable on numeric columns')

    def _compute(self, *args, **kwargs):
        ''' Call the CASColumn's _compute method '''
        return self._column._compute(*args, **kwargs)

    def _get_re_flags(self, flags, case=True):
        ''' Convert regex flags to strings '''
        re_flags = ''
        if flags & re.IGNORECASE or not case:
            re_flags += 'i'
        if flags & re.LOCALE:
            re_flags += 'l'
        if flags & re.MULTILINE:
            re_flags += 'm'
        if flags & re.DOTALL:
            re_flags += 's'
        if flags & re.UNICODE:
            re_flags += 'u'
        if flags & re.VERBOSE:
            re_flags += 'x'
        return re_flags

    def capitalize(self):
        ''' Capitalize first letter, lowercase the rest '''
        return self._compute('capitalize',
                             'upcase(substr({value}, 1, 1)) || ' +
                             'lowcase(substr({value}, 2))',
                             add_length=True)

#   def cat(self, others=None, sep=None, na_rep=None, **kwargs):
#       ''' Concatenate values with given separator '''
#       return self._column._fetch().iloc[:, 0].str.cat(others=others, sep=sep,
#                                                       na_rep=na_rep, **kwargs)
#   def center(self, width, fillchar=' '):
#       return self._compute('center', 'put({value}, ${width}., -c)', width=width)

    def contains(self, pat, case=True, flags=0, na=np.nan, regex=True):
        ''' Return booleans indicating whether or not the pattern exists '''
        if regex:
            if isinstance(pat, CASColumn):
                return self._compute('regex',
                                     r"prxmatch('/' || {pat} || '/%s', {value}) > 0" %
                                     self._get_re_flags(flags, case=case),
                                     pat=pat)
            return self._compute('regex',
                                 r"prxmatch('/{pat}/{flags}', {value}) > 0",
                                 pat=pat, flags=self._get_re_flags(flags, case=case),
                                 use_quotes=False)
        if case:
            return self._compute('contains', 'index({value}, {pat}) > 0',
                                 pat=pat)
        return self._compute('icontains',
                             'index(lowcase({value}), lowcase({pat})) > 0',
                             pat=pat)

    def count(self, pat, flags=0, **kwargs):
        ''' Count occurrences of pattern in each value '''
        if flags & re.IGNORECASE:
            return self._compute('count', 'count({value}, {pat})', pat=pat)
        return self._compute('count', 'count(lowcase({value}), lowcase({pat}))', pat=pat)

    def endswith(self, pat, case=True, flags=0, na=np.nan, regex=True):
        '''
        Does the table column end with `arg`?

        Parameters
        ----------
        pat : CASColumn or string
            The string to compare to

        Returns
        -------
        CASColumn

        '''
        return self._compute('endswith',
                             r"prxmatch('/{pat}\s*$/{flags}', {value}) > 0",
                             pat=pat, flags=self._get_re_flags(flags, case=case),
                             use_quotes=False)

    def find(self, sub, start=0, end=None):
        ''' Return lowest index of pattern in each value '''
        if end is None:
            return self._compute('find',
                                 'find({value}, {sub}, {start}) - 1',
                                 sub=sub, start=start + 1)
        return self._compute('find',
                             'find(substr({value}, 1, {end}), {sub}, {start}) - 1',
                             sub=sub, start=start + 1, end=end - start + 1)

    def index(self, sub, start=0, end=None):
        ''' Return lowest index of pattern in each value '''
        col = self.find(sub, start=start, end=end)
        if col[col < 0]._numrows:
            raise ValueError('substring not found')
        return col

    def len(self):
        ''' Compute the length of each value '''
        return self._compute('len', 'lengthn({value})')

    def lower(self):
        ''' Lowercase the value '''
        return self._compute('lower', 'lowcase({value})', add_length=True)

    def lstrip(self, to_strip=None):
        ''' Strip leading spaces '''
        return self._compute('lstrip', 'strip({value})', add_length=True)

    def repeat(self, repeats):
        ''' Duplicate value the specified number of times '''
        trim = ''
        if not re.match(r'^_\w+_[A-Za-z0-9]+_$', self._column.name):
            trim = 'trim'
        return self._compute('repeat', 'repeat(%s({value}), {repeats}-1)' % trim,
                             repeats=repeats, add_length=True)

    def replace(self, pat, repl, n=-1, case=True, flags=0):
        ''' Replace a pattern in the data '''
        if isinstance(pat, CASColumn) and isinstance(repl, CASColumn):
            rgx = "prxchange('s/'|| trim({pat}) ||'/' trim({repl}) || '/%s',{n},{value})"
        elif isinstance(pat, CASColumn):
            rgx = "prxchange('s/' || trim({pat}) || '/{repl}/%s',{n},{value})"
        elif isinstance(repl, CASColumn):
            rgx = "prxchange('s/{pat}/' || trim({repl}) || '/%s',{n},{value})"
        else:
            rgx = "prxchange('s/{pat}/{repl}/%s',{n},{value})"
        return self._compute('replace', rgx % self._get_re_flags(flags, case=case),
                             pat=pat, repl=repl, n=n, use_quotes=False, add_length=True)

    def rfind(self, sub, start=0, end=None):
        ''' Return highest index of the pattern '''
        # TODO: start / end
        return self._compute('find',
                             'find({value}, {sub}, -lengthn({value})-1) - 1',
                             sub=sub, start=start + 1)

    def rindex(self, sub, start=0, end=None):
        ''' Return highest index of the pattern '''
        # TODO: start / end
        col = self.rfind(sub, start=start, end=end)
        if col[col < 0]._numrows:
            raise ValueError('substring not found')
        return col

    def rstrip(self, to_strip=None):
        ''' Strip trailing whitespace '''
        return self._compute('rstrip', 'trimn({value})', add_length=True)

    def slice(self, start=0, stop=None, step=None):
        ''' Slice a substring from the value '''
        # TODO: step
        if stop is None:
            stop = 'lengthn({value})+1'
        else:
            stop = stop + 1
        return self._compute('slice', 'substr({value}, {start}, {stop}-{start})',
                             start=start + 1, stop=stop, add_length=True)

    def startswith(self, pat, case=True, flags=0, na=np.nan, regex=True):
        '''
        Does the table column start with `pat`?

        Parameters
        ----------
        pat : CASColumn or string
            The string to compare to

        Returns
        -------
        CASColumn

        '''
        return self._compute('startswith',
                             "prxmatch('/^{pat}/{flags}', {value}) > 0",
                             pat=pat, flags=self._get_re_flags(flags, case=case),
                             use_quotes=False)

    def strip(self, to_strip=None):
        ''' Strip leading and trailing whitespace '''
        return self._compute('strip', 'strip({value})', add_length=True)

    def title(self):
        ''' Capitalize each word in the value '''
        return self._compute('title', 'propcase({value})', add_length=True)

    def upper(self):
        ''' Uppercase the value '''
        return self._compute('upper', 'upcase({value})', add_length=True)

    def isalnum(self):
        ''' Does the value contain only alphanumeric characters? '''
        return self._compute('isalnum', 'notalnum({value}) < 1')

    def isalpha(self):
        ''' Does the value contain only alpha characters? '''
        return self._compute('isalpha', 'notalpha({value}) < 1')

    def isdigit(self):
        ''' Does the value contain only digits? '''
        return self._compute('isdigit', 'notdigit({value}) < 1')

    def isspace(self):
        ''' Does the value contain only whitespace? '''
        return self._compute('isspace', 'notspace({value}) < 1')

    def islower(self):
        ''' Does the value contain only lowercase characters? '''
        return self._compute('islower', '(lowcase({value}) = {value})')

    def isupper(self):
        ''' Does the value contain only uppercase characters? '''
        return self._compute('isupper', '(upcase({value}) = {value})')

    def istitle(self):
        ''' Is the value equivalent to the title representation? '''
        return self._compute('istitle', '(propcase({value}) = {value})')

    def isnumeric(self):
        ''' Does the value contain a numeric representation? '''
        return self._compute('isnumeric', r"prxmatch('/^\s*\d+\s*$/', {value}) > 0")

    def isdecimal(self):
        ''' Does the value contain a decimal representation? '''
        return self._compute('isnumeric',
                             r"prxmatch('/^\s*(0?\.\d+|\d+(\.\d*)?)\s*$/', " +
                             r"{value}) > 0")

#   def soundslike(self, arg):
#       '''
#       Does the table column sound like `arg`?

#       Parameters
#       ----------
#       arg : CASColumn or string
#           The string to compare to

#       Returns
#       -------
#       CASColumn

#       '''
#       return self._compute('soundslike', '({value} =* {arg})', arg=arg)


class DatetimeColumnMethods(object):
    ''' CASColumn datetime methods '''

    def __init__(self, column):
        self._column = column
        self._dtype = column.dtype
        if self._dtype not in ['date', 'datetime', 'time']:
            raise TypeError('datetime methods are only usable on CAS dates, ' +
                            'times, and datetimes')

    def _compute(self, *args, **kwargs):
        ''' Call the _compute method on the table column '''
        return self._column._compute(*args, **kwargs)

    def _get_part(self, func):
        ''' Get the specified part of the datetime '''
        if self._dtype == 'date':
            if func in ['hour', 'minute']:
                return self._compute(func, '0')
            return self._compute(func, '%s({value})' % func)
        if self._dtype == 'time':
            if func in ['hour', 'minute']:
                return self._compute(func, '%s({value})' % func)
            return self._compute(func, '%s(today())' % func)
        if func in ['month', 'day', 'year', 'week', 'qtr']:
            return self._compute(func, '%s(datepart({value}))' % func)
        return self._compute(func, '%s({value})' % func)

    @property
    def year(self):
        ''' The year of the datetime '''
        return self._get_part('year')

    @property
    def month(self):
        ''' The month of the datetime January=1, December=12 '''
        return self._get_part('month')

    @property
    def day(self):
        ''' The day of the datetime '''
        return self._get_part('day')

    @property
    def hour(self):
        ''' The hour of the datetime '''
        return self._get_part('hour')

    @property
    def minute(self):
        ''' The minute of the datetime '''
        return self._get_part('minute')

    @property
    def second(self):
        ''' The second of the datetime '''
        if self._dtype == 'date':
            return self._compute('second', '0')
        return self._compute('second', 'int(second({value}))')

    @property
    def microsecond(self):
        ''' The microsecond of the datetime '''
        if self._dtype == 'date':
            return self._compute('microsecond', '0')
        return self._compute('microsecond', 'int(mod(second({value}), 1) * 1000000)')

    @property
    def nanosecond(self):
        ''' The nanosecond of the datetime (always zero) '''
        return self._compute('nanosecond', '0')

    def _get_date(self):
        ''' Return an expression that will return the date only '''
        if self._dtype == 'date':
            return '{value}'
        if self._dtype == 'time':
            return 'today()'
        return 'datepart({value})'

    @property
    def week(self):
        ''' The week ordinal of the year '''
        return self._compute('week', 'week(%s, "v")' % self._get_date())

    @property
    def weekofyear(self):
        ''' The week ordinal of the year '''
        return self.week

    @property
    def dayofweek(self):
        ''' The day of the week (Monday=0, Sunday=6) '''
        return self._compute('weekday', 'weekday(%s) - 2' % self._get_date())

    @property
    def weekday(self):
        ''' The day of the week (Monday=0, Sunday=6) '''
        return self.dayofweek

    @property
    def dayofyear(self):
        ''' The ordinal day of the year '''
        return self._compute('dayofyear', 'mod(juldate(%s), 1000.)' % self._get_date())

    @property
    def quarter(self):
        ''' The quarter of the date '''
        return self._get_part('qtr')

    @property
    def is_month_start(self):
        ''' Logical indicating if first day of the month '''
        return self._compute('is_month_start', '(day(%s) = 1)' % self._get_date())

    @property
    def is_month_end(self):
        ''' Logical indicating if last day of the month '''
        return self._compute('is_month_end', '(intnx("month", %s, 0, "e") = %s)' %
                             (self._get_date(), self._get_date()))

    @property
    def is_quarter_start(self):
        ''' Logical indicating if first day of quarter '''
        return self._compute('is_quarter_start', '(intnx("qtr", %s, 0, "b") = %s)' %
                             (self._get_date(), self._get_date()))

    @property
    def is_quarter_end(self):
        ''' Logical indicating if last day of the quarter '''
        return self._compute('is_quarter_end', '(intnx("qtr", %s, 0, "e") = %s)' %
                             (self._get_date(), self._get_date()))

    @property
    def is_year_start(self):
        ''' Logical indicating if first day of the year '''
        return self._compute('is_year_start', '(intnx("year", %s, 0, "b") = %s)' %
                             (self._get_date(), self._get_date()))

    @property
    def is_year_end(self):
        ''' Logical indicating if the last day of the year '''
        return self._compute('is_year_end', '(intnx("year", %s, 0, "e") = %s)' %
                             (self._get_date(), self._get_date()))

    @property
    def daysinmonth(self):
        ''' The number of days in the month '''
        return self._compute('daysinmonth', 'day(intnx("month", %s, 0, "e"))' %
                             self._get_date())

    @property
    def days_in_month(self):
        ''' The number of days in the month '''
        return self.daysinmonth


class CASColumn(CASTable):
    '''
    Special subclass of CASTable for holding single columns

    '''

    @getattr_safe_property
    def str(self):
        ''' Accessor for string methods '''
        return CharacterColumnMethods(self)

    @getattr_safe_property
    def dt(self):
        ''' Accessor for the datetime methods '''
        return DatetimeColumnMethods(self)

    @getattr_safe_property
    def name(self):
        ''' Return the column name '''
        name = self.get_param('varlist')
        if isinstance(name, items_types):
            name = name[0]
        return name

    @getattr_safe_property
    def dtype(self):
        ''' The data type of the underlying data '''
        return self._columninfo['Type'][0]

    @getattr_safe_property
    def ftype(self):
        ''' The data type and whether it is sparse or dense '''
        return self._columninfo['Type'][0] + ':dense'

    def xs(self, *args, **kwargs):
        ''' Only exists for CASTable '''
        raise AttributeError('xs')

    @getattr_safe_property
    def values(self):
        ''' Return column data as numpy.ndarray '''
        return self._fetch().ix[:, 0].values

    @getattr_safe_property
    def shape(self):
        ''' Return a tuple of the shape of the underlying data '''
        return (self._numrows,)

    @getattr_safe_property
    def ndim(self):
        ''' Return the number of dimensions of the underlying data '''
        return 1

    @getattr_safe_property
    def axes(self):
        ''' Return the row axis labels and column axis labels '''
        # TODO: Create an index proxy object
        return [[]]

    @getattr_safe_property
    def size(self):
        ''' Return the number of elements in the underlying data '''
        return self._numrows

    @getattr_safe_property
    def itemsize(self):
        ''' Return the size of the data type of the underlying data '''
        return self._columninfo['RawLength'][0]

    def isnull(self):
        ''' Return a boolean CASColumn indicating if the values are null '''
        return self._compute('isnull', 'missing({value})')

    def notnull(self):
        ''' Return a boolean CASColumn indicating if the values are not null '''
        return self._compute('notnull', '(missing({value}) = 0)')

    def get(self, key, default=None):
        ''' Get item from CASColumn for the given key '''
        out = self._fetch(from_=key + 1, to=key + 1)
        try:
            return out.get_value(0, self.varlist[0])
        except KeyError:
            pass
        return default

    def sort_values(self, axis=0, ascending=True, inplace=False,
                    kind='quicksort', na_position='last'):
        return CASTable.sort_values(self, self.name, axis=axis, ascending=ascending,
                                    inplace=inplace, kind=kind, na_position=na_position)

    sort = sort_values

    def __iter__(self):
        for item in self._generic_iter('itertuples', index=False):
            yield item[0]

    def iteritems(self, chunksize=None):
        ''' Lazily iterate over (index, value) tuples '''
        return self._generic_iter('itertuples', index=True, chunksize=chunksize)

    def _is_numeric(self):
        ''' Return boolean indicating if the data type is numeric '''
        return self.dtype not in set(['char', 'varchar', 'binary', 'varbinary'])

    def _is_character(self):
        ''' Return boolean indicating if the data type is character '''
        return self.dtype in set(['char', 'varchar', 'binary', 'varbinary'])

    def tolist(self):
        ''' Return a list of the column values '''
        return self._fetch().ix[:, 0].tolist()

    def head(self, n=5):
        ''' Return first `n` rows of the column in a Series '''
        return self.slice(start=0, stop=n - 1)

    def tail(self, n=5):
        ''' Return last `n` rows of the column in a Series '''
        return self.slice(start=-n, stop=-1)

    def slice(self, start=0, stop=None):
        ''' Return from rows from `start` to `stop` in a Series '''
        return CASTable.slice(self, start=start, stop=stop)[self.name]

    def add(self, other, level=None, fill_value=None, axis=0):
        ''' Addition of CASColumn with other, element-wise '''
        if self._is_character():
            trim_value = ''
            trim_other = ''
            if not re.match(r'^_\w+_[A-Za-z0-9]+_$', self.name):
                trim_value = 'trim'
            if isinstance(other, CASColumn):
                if not re.match(r'^_\w+_[A-Za-z0-9]+_$', other.name):
                    trim_other = 'trim'
            return self._compute('add', '%s({value}) || %s({other})' %
                                 (trim_value, trim_other),
                                 other=other, add_length=True)
        return self._compute('add', '({value}) + ({other})', other=other)

    def __add__(self, other):
        return self.add(other)

    def sub(self, other, level=None, fill_value=None, axis=0):
        ''' Subtraction of CASColumn with other, element-wise '''
        if self._is_character():
            raise AttributeError('sub')
        return self._compute('sub', '({value}) - ({other})', other=other)

    def __sub__(self, other):
        return self.sub(other)

    def mul(self, other, level=None, fill_value=None, axis=0):
        ''' Multiplication of CASColumn with other, element-wise '''
        if self._is_character():
            return self.str.repeat(other)
        return self._compute('mul', '({value}) * ({other})', other=other)

    def __mul__(self, other):
        return self.mul(other)

    def div(self, other, level=None, fill_value=None, axis=0):
        ''' Floating division of CASColumn and other, element-wise '''
        if self._is_character():
            raise AttributeError('div')
        return self._compute('div', '({value}) / ({other})', other=other)

    def __div__(self, other):
        return self.div(other)

    def truediv(self, other, level=None, fill_value=None, axis=0):
        ''' Floating division of CASColumn and other, element-wise '''
        return self.div(other, level=level, fill_value=fill_value, axis=axis)

    def __truediv__(self, other):
        return self.div(other)

    def floordiv(self, other, level=None, fill_value=None, axis=0):
        ''' Integer division of CASColumn and other, element-wise '''
        if self._is_character():
            raise AttributeError('floordiv')
        return self._compute('div', 'floor(({value}) / ({other}))', other=other)

    def __floordiv__(self, other):
        return self.floordiv(other)

    def __floor__(self, other):
        if self._is_character():
            raise AttributeError('floor')
        return self._compute('floor', 'floor({value})', other=other)

    def __ceil__(self, other):
        if self._is_character():
            raise AttributeError('ceil')
        return self._compute('ceil', 'ceil({value})', other=other)

    def __trunc__(self, other):
        if self._is_character():
            raise AttributeError('trunc')
        return self._compute('trunc', 'int({value})', other=other)

    def mod(self, other, level=None, fill_value=None, axis=0):
        ''' Modulo of CASColumn and other, element-wise '''
        if self._is_character():
            raise AttributeError('mod')
        return self._compute('mod', 'mod({value}, {other})', other=other)

    def __mod__(self, other):
        return self.mod(other)

    def pow(self, other, level=None, fill_value=None, axis=0):
        ''' Exponential power of CASColumn and other, element-wise '''
        if self._is_character():
            raise AttributeError('pow')
        return self._compute('pow', '({value})**({other})', other=other)

    def __pow__(self, other):
        return self.pow(other)

    def radd(self, other, level=None, fill_value=None, axis=0):
        ''' Addition of CASColumn and other, element-wise '''
        if self._is_character():
            return self._compute('radd', '{other} || {value}', other=other,
                                 add_length=True)
        return self._compute('radd', '({other}) + ({value})', other=other)

    def rsub(self, other, level=None, fill_value=None, axis=0):
        ''' Subtraction of CASColumn and other, element-wise '''
        if self._is_character():
            raise AttributeError('rsub')
        return self._compute('rsub', '({other}) - ({value})', other=other)

    def rmul(self, other, level=None, fill_value=None, axis=0):
        ''' Multiplication of CASColumn and other, element-wise '''
        if self._is_character():
            return self.str.repeat(other)
        return self._compute('rmul', '({other}) * ({value})', other=other)

    def rdiv(self, other, level=None, fill_value=None, axis=0):
        ''' Floating division of CASColumn and other, element-wise '''
        if self._is_character():
            raise AttributeError('rdiv')
        return self._compute('rdiv', '({other}) / ({value})', other=other)

    def rtruediv(self, other, level=None, fill_value=None, axis=0):
        ''' Floating division of CASColumn and other, element-wise '''
        if self._is_character():
            raise AttributeError('rtruediv')
        return self._compute('rtruediv', '({other}) / ({value})', other=other)

    def rfloordiv(self, other, level=None, fill_value=None, axis=0):
        ''' Integere division of CASColumn and other, element-wise '''
        if self._is_character():
            raise AttributeError('floordiv')
        return self._compute('div', 'floor(({other}) / ({value}))', other=other)

    def rmod(self, other, level=None, fill_value=None, axis=0):
        ''' Modulo of CASColumn and other, element-wise '''
        if self._is_character():
            raise AttributeError('rmod')
        return self._compute('rmod', 'mod({other}, {value})', other=other)

    def rpow(self, other, level=None, fill_value=None, axis=0):
        ''' Exponential power of CASColumn and other, element-wise '''
        if self._is_character():
            raise AttributeError('rpow')
        return self._compute('rpow', '({other})**({value})', other=other)

    def round(self, decimals=0, out=None):
        ''' Round each value of the CASColumn to the given number of decimals '''
        if self._is_character():
            raise AttributeError('round')
        if decimals <= 0:
            decimals = 1
        else:
            decimals = 1 / (decimals * 10.)
        return self._compute('round', 'rounde({value}, {decimals})', decimals=decimals)

    def __neg__(self):
        if self._is_character():
            raise AttributeError('__neg__')
        return self._compute('negate', '(-({value}))')

    def __pos__(self):
        if self._is_character():
            raise AttributeError('__pos__')
        return self._compute('pos', '(+({value}))')

    def lt(self, other, axis=0):
        ''' Less-than comparison of CASColumn and other, element-wise '''
        return self._compare('<', other)

    def __lt__(self, other):
        return self.lt(other)

    def gt(self, other, axis=0):
        ''' Greater-than comparison of CASColumn and other, element-wise '''
        return self._compare('>', other)

    def __gt__(self, other):
        return self.gt(other)

    def le(self, other, axis=0):
        ''' Less-than-or-equal-to comparison of CASColumn and other, element-wise '''
        return self._compare('<=', other)

    def __le__(self, other):
        return self.le(other)

    def ge(self, other, axis=0):
        ''' Greater-than-or-equal-to comparison of CASColumn and other, element-wise '''
        return self._compare('>=', other)

    def __ge__(self, other):
        return self.ge(other)

    def ne(self, other, axis=0):
        ''' Not-equal-to comparison of CASColumn and other, element-wise '''
        return self._compare('^=', other)

    def __ne__(self, other):
        return self.ne(other)

    def eq(self, other, axis=0):
        ''' Equal-to comparison of CASColumn and other, element-wise '''
        return self._compare('=', other)

    def __eq__(self, other):
        return self.eq(other)

    def isin(self, values):
        ''' Return a boolean CASColumn indicating if the value is in the given values '''
        if not isinstance(values, items_types):
            values = [values]
        return self._compute('isin', '({value} in {values})', values=values)

    def __invert__(self):
        return self._compute('invert', '(^({value}))')

    def _compute(self, funcname, code, use_quotes=True, extra_compvars=None,
                 extra_comppgm=None, add_length=False, dtype=None, **kwargs):
        '''
        Create a computed column from given expression

        Parameters
        ----------
        funcname : string
            Name for the function
        code : string
            Python string template containing computed column
            expression.  {value} will be replaced by the value
            of the current column.  All string template variables
            will be populated by `kwargs`.
        use_quotes : boolean, optional
            Use quotes around string literals
        extra_compvars : list, optional
            Additional computed variables
        extra_comppgm : string, optional
            Additional computed program
        add_length : boolean, optional
            Add a 'length varname varchar(*)' for the output variable
        dtype : string, optional
            The output data type for the computed value

        Returns
        -------
        CASColumn object

        '''
        out = copy.deepcopy(self)

        outname = '_%s_%s_' % (funcname, self.get_connection()._gen_id())

        out.set_param('varlist', [outname])
        if outname in self.get_param('compvars', []):
            return out

        kwargs = kwargs.copy()

        compvars = [outname]
        comppgm = []

        if dtype:
            comppgm.append('length %s %s' % (_nlit(outname), dtype))
        elif add_length:
            comppgm.append('length %s varchar(*)' % _nlit(outname))

        if extra_compvars:
            compvars.append(extra_compvars)
        if extra_comppgm:
            comppgm.append(extra_comppgm)

        for key, value in six.iteritems(kwargs):
            if isinstance(value, CASColumn):
                aexpr, acompvars, acomppgm = value._to_expression()
                compvars.append(acompvars)
                comppgm.append(acomppgm)
                kwargs[key] = aexpr
            elif use_quotes and (isinstance(value, text_types) or
                                 isinstance(value, binary_types)):
                kwargs[key] = '"%s"' % _escape_string(value)
            elif isinstance(value, items_types):
                items = []
                for item in value:
                    if isinstance(item, CASColumn):
                        aexpr, acompvars, acomppgm = item._to_expression()
                        compvars.append(acompvars)
                        comppgm.append(acomppgm)
                        items.append(aexpr)
                    elif isinstance(item, text_types) or \
                            isinstance(item, binary_types):
                        items.append('"%s"' % _escape_string(item))
                    else:
                        items.append(str(item))
                if items:
                    kwargs[key] = '(%s)' % ', '.join(items)
            else:
                kwargs[key] = str(value)

        kwargs['value'] = _nlit(self.name)
        kwargs['out'] = _nlit(outname)

        if '{out}' not in code:
            code = '{out} = %s' % code

        if not re.search(r';\s*$', code):
            code = '%s; ' % code

        comppgm.append(code.format(**kwargs))

        out.append_computed_columns(compvars, comppgm)

        return out

    def _to_expression(self):
        ''' Convert CASColumn to an expression '''
        return (_nlit(self.name),
                self.get_param('compvars', []),
                self.get_param('comppgm', ''))

    def __and__(self, arg):
        return self._compare('and', arg)

    def __or__(self, arg):
        return self._compare('or', arg)

    def _compare(self, operator, other):
        ''' Compare CASColumn to other using given operator '''
        left = self
        right = other

        # Left side
        left, lcompvars, lcomppgm = left._to_expression()

        compvars = []
        comppgm = []

        # Right side
        if isinstance(right, CASColumn):
            right, rcompvars, rcomppgm = right._to_expression()
            compvars.append(rcompvars)
            comppgm.append(rcomppgm)
        elif isinstance(right, text_types) or isinstance(right, binary_types):
            right = repr(right)

        opname = OPERATOR_NAMES.get(operator, operator)
        col = self._compute(opname, '(%s %s %s)' % (str(left), operator, str(right)),
                            extra_compvars=compvars, extra_comppgm=comppgm)
        return col

    def abs(self):
        ''' Return absolute values element-wise '''
        if self._is_character():
            raise TypeError("bad operand type for abs(): 'str'")
        return self._compute('abs', 'abs({value})')

    def all(self, axis=None, bool_only=None, skipna=None, level=None, **kwargs):
        ''' Return whether all elements are True '''
        numrows = self._numrows
        col = self.copy(deep=True)
        if self._is_character():
            col.append_where('lengthn(%s) ^= 0' % _nlit(col.name))
        else:
            col.append_where('(%s) ^= 0' % _nlit(col.name))
        return col._numrows == numrows

    def any(self, axis=None, bool_only=None, skipna=None, level=None, **kwargs):
        ''' Return whether any elements are True '''
        col = self.copy(deep=True)
        if self._is_character():
            col.append_where('lengthn(%s) ^= 0' % _nlit(col.name))
        else:
            col.append_where('(%s) ^= 0' % _nlit(col.name))
        return col._numrows > 0

    def between(self, left, right, inclusive=True):
        ''' Return boolean CASColumn equivalent to left <= value <= right '''
        if inclusive:
            return self._compute('between',
                                 '({left} <= {value}) and ({value} <= {right})',
                                 left=left, right=right)
        return self._compute('between', '({left} < {value}) and ({value} < {right})',
                             left=left, right=right)

    def clip(self, lower=None, upper=None, out=None, axis=0):
        ''' Trim values at input threshold(s) '''
        if lower is not None and upper is not None:
            return self._compute('clip', 'min({upper}, max({lower}, {value}))',
                                 upper=upper, lower=lower)
        elif lower is not None:
            return self._compute('clip_lower', 'max({lower}, {value})', lower=lower)
        elif upper is not None:
            return self._compute('clip_upper', 'min({upper}, {value})', upper=upper)
        return self.copy(deep=True)

    def clip_lower(self, threshold, axis=0):
        ''' Trim values below given threshold '''
        return self.clip(lower=threshold)

    def clip_upper(self, threshold, axis=0):
        ''' Trim values above given threshold '''
        return self.clip(upper=threshold)

    def _to_table(self):
        ''' Convert CASColumn object to a CASTable object '''
        column = copy.deepcopy(self)

        table = CASTable(**column.to_params())

        for key, value in six.iteritems(column._action_params):
            table._action_params[key] = value

        try:
            table.set_connection(column.get_connection())
        except SWATError:
            pass

        return table

    def _combine(self, *others):
        ''' Combine CASColumn objects into a CASTable object '''
        tbl = self._to_table()
        for item in others:
            tbl.append_varlist(item.get_param('varlist', []))
            tbl.append_compvars(item.get_param('compvars', []))
            tbl.append_comppgm(item.get_param('comppgm', ''))
            tbl.append_where(item.get_param('where', ''))
        if not tbl.get_param('compvars', None):
            tbl.del_param('compvars')
        if not tbl.get_param('comppgm', None):
            tbl.del_param('comppgm')
        if not tbl.get_param('where', None):
            tbl.del_param('where')
        return tbl

    def corr(self, other, method='pearson', min_periods=None):
        ''' Compute correlation with other column '''
        return self._combine(other).corr().iloc[0, 1]

    def count(self, level=None):
        ''' Return the number of non-NA/null observations in the CASColumn '''
        out = CASTable.count(self, level=level)
        if isinstance(out, pd.DataFrame):
            return out[self.name].astype(np.int64)
        return out.iat[0]

    def describe(self, percentiles=None, include=None, exclude=None, stats=None):
        ''' Generate various summary statistics '''
        return CASTable.describe(self, percentiles=percentiles, include=include,
                                 exclude=exclude, stats=stats).ix[:, 0]

    def _get_summary_stat(self, name):
        '''
        Run simple.summary and get the given statistic

        Parameters
        ----------
        name : string
            The name of the simple.summary column

        Returns
        -------
        Series
            for single index output
        DataFrame
            for multi-index output

        '''
        return CASTable._get_summary_stat(self, name)[self.name]

    def max(self, axis=None, skipna=True, level=None, **kwargs):
        ''' Return the maximum value '''
        out = self._topk_values('max', axis=axis, skipna=skipna, level=level,
                                **kwargs)
        if self.get_groupby_vars():
            return out[self.name]
        return out.at[self.name]

    def mean(self, axis=None, skipna=True, level=None, **kwargs):
        ''' Return the mean value '''
        return self._get_summary_stat('mean')

    def median(self, q=0.5, axis=0, interpolation='nearest'):
        ''' Return the median value '''
        return self.quantile(0.5, axis=axis, interpolation='nearest')

    def min(self, axis=None, skipna=True, level=None, **kwargs):
        ''' Return the minimum value '''
        out = self._topk_values('min', axis=axis, skipna=skipna, level=level,
                                **kwargs)
        if self.get_groupby_vars():
            return out[self.name]
        return out.at[self.name]

    def mode(self, axis=0, max_tie=100):
        ''' Return the mode values '''
        return CASTable.mode(self, axis=axis, max_tie=max_tie)[self.name]

    def quantile(self, q=0.5, axis=0, interpolation='nearest'):
        ''' Return the value at the given quantile '''
        return CASTable.quantile(self, q=q, axis=axis, numeric_only=False,
                                 interpolation=interpolation)[self.name]

    def sum(self, axis=None, skipna=None, level=None):
        ''' Return the sum of the values '''
        return self._get_summary_stat('sum')

    def nlargest(self, n=5, keep='first'):
        ''' Return the n largest values '''
        col = self.copy(deep=True)
        col._action_params = {}
        return col._fetch(from_=1, to=n, sortby=[dict(name=col.name,
                          order='DESCENDING', formatted='RAW')])[col.name]

    def nsmallest(self, n=5, keep='first'):
        ''' Return the n smallest values '''
        col = self.copy(deep=True)
        col._action_params = {}
        return col._fetch(from_=1, to=n, sortby=[dict(name=col.name,
                          order='ASCENDING', formatted='RAW')])[col.name]

    def std(self, axis=None, skipna=None, level=None, ddof=1):
        ''' Return the standard deviation of the values '''
        return self._get_summary_stat('std')

    def var(self, axis=None, skipna=None, level=None, ddof=1):
        ''' Return the unbiased variance of the values '''
        return self._get_summary_stat('var')

    def unique(self):
        ''' Return array of unique values in the CASColumn '''
        tmpname = str(uuid.uuid4())
        out = self._frequencies(includemissing=True)

        if len(out.index.names) > 1:
            names = list(out.index.names)
            out.name = tmpname
            var = names.pop()
            out = out.reset_index()
            del out[tmpname]
            return out.groupby(names)[var].unique()

        return pd.Series(out.index, name=self.name).as_matrix()

    def nunique(self, dropna=True):
        ''' Return number of unique elements in the CASColumn '''
        return self._topk_values('unique', skipna=dropna)[self.name]

    @getattr_safe_property
    def is_unique(self):
        ''' Return boolean indicating if the values in the CASColumn are unique '''
        is_unique = self.value_counts(dropna=False) == 1
        if self.get_groupby_vars():
            return is_unique
        return is_unique.iat[0]

    def _frequencies(self, includemissing=False):
        '''
        Compute frequencies taking groupby into account

        Parameters
        ----------
        includemissing: boolean, optional
            Should missing values be included in the frequency counts?

        Returns
        -------
        Series

        '''
        out = self._retrieve('simple.freq',
                             includemissing=includemissing)['Frequency']

        if 'CharVar' in out.columns:
            out.rename(columns=dict(CharVar=self.name), inplace=True)
        else:
            out.rename(columns=dict(NumVar=self.name), inplace=True)

        out.set_index(self.get_groupby_vars() + [self.name], inplace=True)

        out = out['Frequency'].astype(np.int64)
        out.name = self.name
        return out

    def value_counts(self, normalize=False, sort=True, ascending=False,
                     bins=None, dropna=True):
        ''' Return object containing counts of unique values '''
        tmpname = str(uuid.uuid4())
        out = self._frequencies(includemissing=not dropna)

        # Drop NaN indexes / data
        if dropna:
            indexes = list(out.index.names)
            out.name = tmpname
            out = out.reset_index()
            out.dropna(inplace=True)
            out.set_index(indexes, inplace=True)
            out = out[tmpname]

        # Normalize data / groups to 1
        if normalize:
            groups = self.get_groupby_vars()
            if groups:
                out.name = tmpname
                sum = out.sum(level=list(range(len(out.index.names)-1))).to_frame()
                out = out.reset_index(level=-1)
                out = pd.merge(out, sum, left_index=True, right_index=True, how='inner')
                out[tmpname] = out[tmpname + '_x'] / out[tmpname + '_y']
                out.set_index(self.name, append=True, inplace=True)
                out = out[tmpname]
            else:
                out = out / out.sum()

        # Prep for sorting
        out.name = tmpname
        indexes = list(out.index.names)
        columns = [out.name]
        out = out.to_frame()
        out.reset_index(inplace=True)

        # Sort (at least by the index)
        if sort:
            out.sort_values(indexes[:-1] + columns, inplace=True,
                            ascending=([True] * len(indexes[:-1])) + [ascending])
        else:
            out.sort_values(indexes, inplace=True,
                            ascending=([True] * len(indexes)))

        # Set indexes and names
        out.set_index(indexes, inplace=True)
        out = out[out.columns[0]]
        out.index.name = None
        out.name = None

        return out

    # Not DataFrame methods, but they are available statistics.

    def nmiss(self):
        ''' Return number of missing values '''
        return self._get_summary_stat('nmiss')

    def stderr(self):
        ''' Return standard error of the values '''
        return self._get_summary_stat('stderr')

    def uss(self):
        ''' Return uncorrected sum of squares of the values '''
        return self._get_summary_stat('uss')

    def css(self):
        ''' Return corrected sum of squares of the values '''
        return self._get_summary_stat('css')

    def cv(self):
        ''' Return coefficient of variation of the values '''
        return self._get_summary_stat('cv')

    def tvalue(self):
        ''' Return value of T-statistic for hypothetical testing '''
        return self._get_summary_stat('tvalue')

    def probt(self):
        ''' Return p-value of the T-statistic '''
        return self._get_summary_stat('probt')

    # Serialization / IO / Conversion

    @classmethod
    def from_csv(cls, connection, path, header=0, sep=',', index_col=0, parse_dates=True,
                 tupleize_cols=False, infer_datetime_format=False, **kwargs):
        ''' Create a CASColumn from a CSV file '''
        return connection.read_csv(path, header=header, sep=sep, index_col=index_col,
                                   parse_dates=parse_dates, tupleize_cols=tupleize_cols,
                                   infer_datetime_format=infer_datetime_format,
                                   **kwargs)._to_column()

    def to_series(self, *args, **kwargs):
        ''' Retrieve all elements into a Series '''
        return pd.concat(list(self._retrieve('table.fetch', sastypes=False,
                                            to=MAX_INT64_INDEX,
                                            noindex=True).values()))[self.name]

    def _to_any(self, method, *args, **kwargs):
        ''' Generic converter to various forms '''
        out = pd.concat(list(self._retrieve('table.fetch', sastypes=False,
                                            to=get_option('cas.dataset.max_rows_fetched'),
                                            noindex=True).values()))[self.name]
        return getattr(out, 'to_' + method)(*args, **kwargs)

    def to_frame(self, *args, **kwargs):
        ''' Convert CASColumn to a DataFrame '''
        return self._to_any('frame', *args, **kwargs)

    def to_xarray(self, *args, **kwargs):
        ''' Return an xarray object from the CASColumn '''
        return self._to_any('xarray', *args, **kwargs)


class CASTableGroupBy(object):
    '''
    Group CASTable / CASColumn objects by specified values

    Parameters
    ----------
    table : CASTable or CASColumn
        The CASTable / CASColumn to group.
    by : string or list-of-strings
        The column name(s) that specify the group values.
    axis : int, optional
        Unsupported.
    level : int or level-name, optional
        Unsupported.
    as_index : boolean, optional
        If True, the group labels become the index in the output.
    sort : boolean, optional
        If True, the output is sorted by the group keys.
    group_keys : boolean, optional
        Unsupported.
    squeeze : boolean, optional
        Unsupported.

    Returns
    -------
    CASTableGroupBy object

    '''

    def __init__(self, table, by, axis=0, level=None, as_index=True, sort=True,
                 group_keys=True, squeeze=False, **kwargs):
        self._table = table.copy(deep=True)
        self._table.append_groupby(by)
        if isinstance(by, items_types):
            self._by = list(by)
        else:
            self._by = [by]
        self._sort = sort
        self._plot = CASTablePlotter(self._table)
        self._as_index = as_index

    def __iter__(self):
        groupby = self._table[self._by].simple.groupby()['Groupby']
        groupby = groupby[self._by].to_records(index=False)
        for group in groupby:
            yield tuple(group), self.get_group(group)

    def __getattr__(self, name):
        return getattr(self._table, name)

    def get_group(self, name, obj=None):
        '''
        Construct a CASTable / CASColumn with the given group key

        Parameters
        ----------
        name : any or tuple-of-anys
            The groupby value (or tuple of values for multi-level groups)
        obj : CASTable or CASColumn, optional
            The CASTable / CASColumn to use instead of self

        Returns
        -------
        CASTable or CASColumn

        '''
        if obj is None:
            obj = self

        grptbl = obj._table.copy()

        for key, value in zip(self._by, name):

            if pd.isnull(value):
                grptbl.append_where('%s = .' % _nlit(key))
            else:
                if isinstance(value, text_types) or \
                        isinstance(value, binary_types):
                    value = '"%s"' % _escape_string(value)
                else:
                    value = str(value)
                grptbl.append_where('%s = %s' % (_nlit(key), value))

        return grptbl

    def get_groupby_vars(self):
        ''' Get groupby variables from table '''
        return self._table.get_groupby_vars()

    @getattr_safe_property
    def plot(self):
        ''' Plot using groups '''
        return self._plot

    def head(self, *args, **kwargs):
        '''
        Retrieve first values of each group
 
        See CASTable.head / CASColumn.head for arguments.

        '''
        return self._table.head(*args, **kwargs)

    def tail(self, *args, **kwargs):
        '''
        Retrieve last values of each group
 
        See CASTable.tail / CASColumn.tail for arguments.

        '''
        return self._table.tail(*args, **kwargs)

    def slice(self, *args, **kwargs):
        '''
        Retrieve requested values of each group
 
        See CASTable.head / CASColumn.head for arguments.

        '''
        return self._table.slice(*args, **kwargs)

    def to_frame(self, *args, **kwargs):
        ''' 
        Retrieve all values into a DataFrame

        See CASTable.to_frame / CASColumn.to_frame for arguments.

        '''
        return self._table.to_frame(*args, **kwargs)

    def nth(self, n, dropna=None):
        '''
        Return the nth row from each group

        Parameters
        ----------
        n : int or list-of-ints
            The rows to select.

        Returns
        -------
        DataFrame

        '''
        if not isinstance(n, items_types):
            n = [n] 
        out = pd.concat(self.slice(x, x) for x in n)
        if self._as_index:
            return out.set_index(self.get_groupby_vars()).sort_index()
        return out

    def unique(self, *args, **kwargs):
        '''
        Get unique values using groups

        See CASTable.unique / CASColumn.unique for arguments.

        '''
        if self._as_index:
            return self._table.unique(*args, **kwargs)
        return self._table.unique(*args, **kwargs).reset_index(self.get_groupby_vars())

    def nunique(self, *args, **kwargs):
        '''
        Get number of unique values using groups

        See CASTable.unique / CASColumn.unique for arguments.

        '''
        if self._as_index:
            return self._table.nunique(*args, **kwargs)
        return self._table.nunique(*args, **kwargs).reset_index(self.get_groupby_vars())

    def value_counts(self, *args, **kwargs):
        '''
        Get value counts using groups

        See CASTable.unique / CASColumn.unique for arguments.

        '''
        if self._as_index:
            return self._table.value_counts(*args, **kwargs)
        return self._table.value_counts(*args, **kwargs).reset_index(
                   self.get_groupby_vars())

    def __getitem__(self, name):
        return self._table[name]

    def max(self, *args, **kwargs):
        '''
        Get maximum values using groups

        See CASTable.unique / CASColumn.unique for arguments.

        '''
        if self._as_index:
            return self._table.max(*args, **kwargs)
        return self._table.max(*args, **kwargs).reset_index(self.get_groupby_vars())

    def mean(self, *args, **kwargs):
        '''
        Get mean values using groups

        See CASTable.unique / CASColumn.unique for arguments.

        '''
        if self._as_index:
            return self._table.mean(*args, **kwargs)
        return self._table.mean(*args, **kwargs).reset_index(self.get_groupby_vars())

    def min(self, *args, **kwargs):
        '''
        Get minimum values using groups

        See CASTable.unique / CASColumn.unique for arguments.

        '''
        if self._as_index:
            return self._table.min(*args, **kwargs)
        return self._table.min(*args, **kwargs).reset_index(self.get_groupby_vars())

    def median(self, *args, **kwargs):
        '''
        Get median values using groups

        See CASTable.unique / CASColumn.unique for arguments.

        '''
        if self._as_index:
            return self._table.median(*args, **kwargs)
        return self._table.median(*args, **kwargs).reset_index(self.get_groupby_vars())

    def mode(self, *args, **kwargs):
        '''
        Get mode values using groups

        See CASTable.unique / CASColumn.unique for arguments.

        '''
        if self._as_index:
            return self._table.mode(*args, **kwargs)
        return self._table.mode(*args, **kwargs).reset_index(self.get_groupby_vars())

    def quantile(self, *args, **kwargs):
        '''
        Get quantiles using groups

        See CASTable.unique / CASColumn.unique for arguments.

        '''
        if self._as_index:
            return self._table.quantile(*args, **kwargs)
        return self._table.quantile(*args, **kwargs).reset_index(self.get_groupby_vars())

    def sum(self, *args, **kwargs):
        '''
        Get sum using groups

        See CASTable.unique / CASColumn.unique for arguments.

        '''
        if self._as_index:
            return self._table.sum(*args, **kwargs)
        return self._table.sum(*args, **kwargs).reset_index(self.get_groupby_vars())

    def std(self, *args, **kwargs):
        '''
        Get std using groups

        See CASTable.unique / CASColumn.unique for arguments.

        '''
        if self._as_index:
            return self._table.std(*args, **kwargs)
        return self._table.std(*args, **kwargs).reset_index(self.get_groupby_vars())

    def var(self, *args, **kwargs):
        '''
        Get var using groups

        See CASTable.unique / CASColumn.unique for arguments.

        '''
        if self._as_index:
            return self._table.var(*args, **kwargs)
        return self._table.var(*args, **kwargs).reset_index(self.get_groupby_vars())

    def nmiss(self, *args, **kwargs):
        '''
        Get nmiss using groups

        See CASTable.unique / CASColumn.unique for arguments.

        '''
        if self._as_index:
            return self._table.nmiss(*args, **kwargs)
        return self._table.nmiss(*args, **kwargs).reset_index(self.get_groupby_vars())

    def stderr(self, *args, **kwargs):
        '''
        Get stderr using groups

        See CASTable.unique / CASColumn.unique for arguments.

        '''
        if self._as_index:
            return self._table.stderr(*args, **kwargs)
        return self._table.stderr(*args, **kwargs).reset_index(self.get_groupby_vars())

    def uss(self, *args, **kwargs):
        '''
        Get uss using groups

        See CASTable.unique / CASColumn.unique for arguments.

        '''
        if self._as_index:
            return self._table.uss(*args, **kwargs)
        return self._table.uss(*args, **kwargs).reset_index(self.get_groupby_vars())

    def css(self, *args, **kwargs):
        '''
        Get css using groups

        See CASTable.unique / CASColumn.unique for arguments.

        '''
        if self._as_index:
            return self._table.css(*args, **kwargs)
        return self._table.css(*args, **kwargs).reset_index(self.get_groupby_vars())

    def cv(self, *args, **kwargs):
        '''
        Get cv using groups

        See CASTable.unique / CASColumn.unique for arguments.

        '''
        if self._as_index:
            return self._table.cv(*args, **kwargs)
        return self._table.cv(*args, **kwargs).reset_index(self.get_groupby_vars())

    def tvalue(self, *args, **kwargs):
        '''
        Get tvalue using groups

        See CASTable.unique / CASColumn.unique for arguments.

        '''
        if self._as_index:
            return self._table.tvalue(*args, **kwargs)
        return self._table.tvalue(*args, **kwargs).reset_index(self.get_groupby_vars())

    def probt(self, *args, **kwargs):
        '''
        Get probt using groups

        See CASTable.unique / CASColumn.unique for arguments.

        '''
        if self._as_index:
            return self._table.probt(*args, **kwargs)
        return self._table.probt(*args, **kwargs).reset_index(self.get_groupby_vars())

    def describe(self, *args, **kwargs):
        '''
        Get basic statistics using groups

        See CASTable.unique / CASColumn.unique for arguments.

        '''
        if self._as_index:
            return self._table.describe(*args, **kwargs)
        return self._table.describe(*args, **kwargs).reset_index(
                   self.get_groupby_vars())
