# encoding: utf-8
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Author: Kyle Lahnakoski (kyle@lahnakoski.com)
#
from __future__ import absolute_import
from __future__ import division
from __future__ import unicode_literals

from collections import Mapping

from jx_base import NESTED, python_type_to_json_type, OBJECT
from mo_dots import concat_field, split_field, join_field, Data
from mo_future import none_type
from mo_future import sort_using_key, PY2, text_type
from mo_json.typed_encoder import NESTED_TYPE
from mo_logs import Log
from parquet_thrift.parquet.ttypes import Type, FieldRepetitionType, SchemaElement, ConvertedType
from pyLibrary.env.typed_inserter import json_type_to_inserter_type
from thrift_structures import parquet_thrift

REQUIRED = FieldRepetitionType.REQUIRED
OPTIONAL = FieldRepetitionType.OPTIONAL
REPEATED = FieldRepetitionType.REPEATED


class SchemaTree(object):

    def __init__(self, locked=False):
        """
        :param locked: DO NOT ALLOW SCHEMA EXPANSION
        """
        self.element = None
        self.more = {}  # MAP FROM NAME TO MORE SchemaTree
        self.values = {}  # MAP FROM JSON TYPE TO SchemaElement
        self.diff_schema = []  # PLACEHOLDER OR NET-NEW COLUMNS ADDED DURING SCHEMA EXPANSION
        self.locked = locked

    def add(self, name, repetition_type, type):
        """
        :param name: dot delimited path to the property (use dot (".") for none)
        :param repetition_type: one of OPTIONAL or NESTED (REQUIRED is not possible)
        :param json_type: the json type to store
        :return:
        """
        path = split_field(name)
        output = self
        for i, n in enumerate(path[:-1]):
            next = output.more.get(n)
            if next:
                output = next
            else:
                output = output._add_one(join_field(path[0:i + 1]), OPTIONAL, object)
        n = output.more.get(path[-1])
        if n:
            Log.error("can not redefine a property")
        else:
            return output._add_one(name, repetition_type, type)


    def _add_one(self, full_name, repetition_type, type):
        simple_name = split_field(full_name)[-1]
        output = SchemaTree()
        output.locked = self.locked
        ptype, ltype, jtype, itype, length = python_type_to_all_types[type]
        output.element = SchemaElement(
            name=full_name,
            type=ptype,
            type_length=length,
            repetition_type=OPTIONAL,
            converted_type=ltype
        )

        if repetition_type is REPEATED:
            parent1 = self.more[simple_name] = SchemaTree()
            parent1.element = SchemaElement(
                name=full_name,
                repetition_type=OPTIONAL
            )

            if jtype is NESTED:
                Log.error("not quite sure if you are being redundant, or if you want a 2d array")
            elif jtype is OBJECT:
                parent1.more[NESTED_TYPE] = output
            else:
                parent2 = parent1.more[NESTED_TYPE] = SchemaTree()
                parent2.element = SchemaElement(
                    name=full_name,
                    repetition_type=REPEATED
                )
                parent2.values[itype] = output
        elif jtype is OBJECT:
            self.more[full_name] = output
        else:
            parent1 = self.more[simple_name] = SchemaTree()
            parent1.values[itype] = output
        return output


    def __getitem__(self, name):
        def _get(node, path):
            if node.more:
                return _get(node.more[path[0]], path[1:])
            else:
                return node.values[path[0]]

        return _get(self, split_field(name))

    @staticmethod
    def new_instance(parquet_schema):
        index = [0]

        def _worker(start):
            output = SchemaTree()
            root = parquet_schema[index[0]]
            if root.type:
                ptype, ltype, jtype, itype = python_type_to_all_types[root.type]
                output.values[itype] = root
                return output

            output.element = root
            max = start + root.num_children
            while index[0] < max:
                name = join_field(split_field(parquet_schema[index[0]].name)[-1:])
                index[0] += 1
                child = _worker(index[0])
                output.more[name] = child
            return output

        return _worker(0).more['.']

    @property
    def leaves(self):
        return [itype for itype in self.values.keys()] + [
                   concat_field(name, leaf)
                   for name, child_schema in self.more.items()
                   for leaf in child_schema.leaves
               ]

    def get_parquet_metadata(
        self,
        path='.'
    ):
        """
        OUTPUT PARQUET METADATA COLUMNS
        :param path: FOR INTERNAL USE
        :return: LIST OF SchemaElement
        """
        children = []
        for name, child_schema in sort_using_key(self.more.items(), lambda p: p[0]):
            children.extend(child_schema.get_parquet_metadata(concat_field(path, name)))
        children.extend(v for k, v in sort_using_key(self.values.items(), lambda p: p[0]))

        return [parquet_thrift.SchemaElement(
            name=path,
            num_children=len(children)
        )] + children

    def max_definition_level(self):
        if not self.more:
            return 1
        else:
            max_child = [m.max_definition_level() for m in self.more.values()]
            return max(max_child) + 1


def get_length(dtype, value=None):
    if dtype is Type.BYTE_ARRAY:
        if value is None:
            Log.error("we need to guess the length")
        return len(value)
    elif dtype is None:
        return 0
    else:
        return dtype.itemsize


def get_repetition_type(jtype):
    return FieldRepetitionType.REPEATED if jtype is NESTED else FieldRepetitionType.OPTIONAL


def merge_schema_element(element, name, value, ptype, ltype, dtype, jtype, ittype, length):
    if not element:
        output = parquet_thrift.SchemaElement(
            name=name,
            type=dtype,
            converted_type=ltype,
            type_length=length,
            repetition_type=get_repetition_type(jtype)
        )
        return output, True
    else:
        element.type_length = max(element.type_length, length)
        return element, False


all_type_to_parquet_type = {
    none_type: None,
    bool: Type.BOOLEAN,
    text_type: Type.BYTE_ARRAY,
    int: Type.INT64,
    float: Type.DOUBLE,
    dict: None,
    object: None,
    Data: None,
    Mapping: None,
    list: None
}


all_type_to_parquet_logical_type = {
    none_type: None,
    bool: None,
    text_type: ConvertedType.UTF8,
    int: ConvertedType.UINT_64,
    float: None,
    dict: None,
    object: None,
    Data: None,
    Mapping: None,
    list: None
}

all_type_to_length = {
    none_type: None,
    bool: 1,
    text_type: None,
    int: 8,
    float: 8,
    dict: None,
    object: None,
    Data: None,
    Mapping: None,
    list: None
}

if PY2:
    all_type_to_parquet_type[long] = Type.INT64
    all_type_to_parquet_logical_type[long] = ConvertedType.UINT_64
    all_type_to_length[long] = 8


# MAP FROM PYTHON TYPE TO (parquet_type, parquet_logical_type, json_type, inserter_type)
python_type_to_all_types = {
    ptype: (
        dtype,
        all_type_to_parquet_logical_type[ptype],
        python_type_to_json_type[ptype],
        json_type_to_inserter_type.get(python_type_to_json_type[ptype]),
        all_type_to_length[ptype]
    )
    for ptype, dtype in all_type_to_parquet_type.items()
}


