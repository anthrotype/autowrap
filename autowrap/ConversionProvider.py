from collections import namedtuple
import re
from collections import defaultdict
from DeclResolver import ResolvedClass

from Types import CppType
from Code import Code

class ConverterRegistry(object):
    """
    Works with two level lookup: first find converters which support base_type,
    then call .matches on them to find the finally matching converters

    Therefore TypeConverterBase has method .get_base_types and .matches
    """

    def __init__(self):
        self.lookup = defaultdict(list)

    def register(self, converter):
        assert isinstance(converter, TypeConverterBase)
        converter._set_converter_registry(self)
        for base_type in converter.get_base_types():
            self.lookup[base_type].append(converter)

    def get(self, cpp_type):
        rv = [conv for conv in self.lookup[cpp_type.base_type]\
                   if conv.matches(cpp_type)]
        if len(rv)>1:
            raise Exception("multiple converters for %s" % cpp_type)
        if len(rv)<1:
            raise Exception("no converter for %s" % cpp_type)
        return rv[0]

    def set_names_of_classes_to_wrap(self, names_of_classes_to_wrap):
        for converters in self.lookup.values():
            for converter in converters:
                converter.set_names_of_classes_to_wrap(names_of_classes_to_wrap)
        self.names_of_classes_to_wrap = names_of_classes_to_wrap

    def cy_decl_str(self, type_):
        if type_.template_args is not None:
            targs = [self.cy_decl_str(t) for t in type_.template_args]
            targs = "[%s]" % (", ".join(targs))
        else:
            targs = ""

        if type_.base_type in self.names_of_classes_to_wrap:
            base = "_%s" % type_.base_type
        else:
            base = type_.base_type

        if type_.is_ptr:
            base += " * "
        return "%s%s" % (base, targs)




class TypeConverterBase(object):

    def __init__(self):
        self.names_of_classes_to_wrap = None

    def set_names_of_classes_to_wrap(self, names_of_classes_to_wrap):
        self.names_of_classes_to_wrap = names_of_classes_to_wrap

    def _set_converter_registry(self, r):
        self.converters = r

    def get_base_types(self):
        """
        for first level lookup in registry
        """
        raise NotImplementedError()

    def matches(self, cpp_type):
        """
        for second level lookup in registry
        """
        raise NotImplementedError()

    def matching_python_type(self, cpp_type):
        raise NotImplementedError()

    def type_check_expression(self, cpp_type, argument_var):
        raise NotImplementedError()

    def input_conversion(self, cpp_type, argument_var, arg_num):
        raise NotImplementedError()

    def output_conversion(self, cpp_type, input_cpp_var, output_py_var):
        raise NotImplementedError()


class NumberConverter(TypeConverterBase):

    def get_base_types(self):
        return "int", "long"

    def matches(self, cpp_type):
        return not cpp_type.is_ptr

    def matching_python_type(self, cpp_type):
        return "int"

    def type_check_expression(self, cpp_type, argument_var):
        return "isinstance(%s, int)" % (argument_var,)

    def input_conversion(self, cpp_type, argument_var, arg_num):
        code = ""
        call_as = "(<int>%s)" % argument_var
        cleanup = ""
        return code, call_as, cleanup

    def output_conversion(self, cpp_type, input_cpp_var, output_py_var):
        return "%s = <int>%s" % (output_py_var, input_cpp_var)


class CharPtrConverter(TypeConverterBase):

    def get_base_types(self):
        return "char",

    def matches(self, cpp_type):
        return cpp_type.is_ptr

    def matching_python_type(self, cpp_type):
        return "bytes"

    def type_check_expression(self, cpp_type, argument_var):
        return "isinstance(%s, bytes)" % (argument_var,)

    def input_conversion(self, cpp_type, argument_var, arg_num):
        code = ""
        call_as = "(<char *>%s)" % argument_var
        cleanup = ""
        return code, call_as, cleanup


    def output_conversion(self, cpp_type, input_cpp_var, output_py_var):
        return "%s = <char *>%s" % (output_py_var, input_cpp_var)


class TypeToWrapConverter(TypeConverterBase):

    def __init__(self, name_of_class_to_wrap):
        self.name_of_class_to_wrap = name_of_class_to_wrap

    def get_base_types(self):
        return self.name_of_class_to_wrap,

    def matches(self, cpp_type):
        return True

    def matching_python_type(self, cpp_type):
        return cpp_type.base_type

    def type_check_expression(self, cpp_type, argument_var):
        return "isinstance(%s, %s)" % (argument_var, cpp_type.base_type)

    def input_conversion(self, cpp_type, argument_var, arg_num):
        code = ""
        bt = cpp_type.base_type
        if cpp_type.is_ptr:
            call_as = "<_%s *>(%s.inst)" % (bt, argument_var)
        else:
            call_as = "<_%s>deref(%s.inst)" % (bt, argument_var)
        cleanup = ""
        return code, call_as, cleanup


    def output_conversion(self, cpp_type, input_cpp_var, output_py_var):

        assert not cpp_type.is_ptr

        cy_clz = cpp_type.base_type
        return Code().add("""
                      |cdef $cy_clz $output_py_var = $cy_clz.__new__($cy_clz)
                      |$output_py_var.inst = new _$cy_clz($input_cpp_var)
        """, locals())

class StdVectorConverter(TypeConverterBase):

    def get_base_types(self):
        return "std_vector",

    def matches(self, cpp_type):
        return True

    def matching_python_type(self, cpp_type):
        return "list"

    def type_check_expression(self, cpp_type, arg_var):
        tt, = cpp_type.template_args
        inner_conv = self.converters.get(tt)
        assert inner_conv is not None, "arg type %s not supported" % tt
        inner_check = inner_conv.type_check_expression(tt, "li")

        return Code().add("""
          |isinstance($arg_var, list) and all($inner_check for li in $arg_var)
          """, locals()).render()

    def input_conversion(self, cpp_type, argument_var, arg_num):
        tt, = cpp_type.template_args
        if tt.base_type in self.names_of_classes_to_wrap:
            temp_var = "v%d" % arg_num
            code = Code().add("""
                |cdef std_vector[_$tt] * $temp_var = new std_vector[_$tt]()
                |cdef $tt item
                |for item in $argument_var:
                |   $temp_var.push_back(deref(item.inst))
                """, locals())
            if cpp_type.is_ref:
                cleanup_code = Code().add("""
                    |cdef replace = []
                    |cdef std_vector[_$tt].iterator it = $temp_var.begin()
                    |while it != $temp_var.end():
                    |   item = $tt.__new__($tt)
                    |   item.inst = new _$tt(deref(it))
                    |   replace.append(item)
                    |   inc(it)
                    |$argument_var[:] = replace
                    """, locals())
            else:
                cleanup_code = ""
            return code, "deref(%s)" % temp_var, cleanup_code

        else:
            temp_var = "v%d" % arg_num
            code = Code().add("""
                |cdef std_vector[$tt] * $temp_var = new std_vector[$tt]()
                |cdef $tt item
                |for item in $argument_var:
                |   $temp_var.push_back(item)
                """, locals())
            if cpp_type.is_ref:
                cleanup_code = Code().add("""
                    |cdef replace = []
                    |cdef std_vector[$tt].iterator it = $temp_var.begin()
                    |while it != $temp_var.end():
                    |   replace.append(deref(it))
                    |   inc(it)
                    |$argument_var[:] = replace
                    """, locals())
            else:
                cleanup_code = ""
            return code, "deref(%s)" % temp_var, cleanup_code


    def output_conversion(self, cpp_type, input_cpp_var, output_py_var):

        assert not cpp_type.is_ptr

        tt, = cpp_type.template_args
        if tt.base_type in self.names_of_classes_to_wrap:
            code = Code().add("""
                |$output_py_var = []
                |cdef std_vector[_$tt].iterator it = $input_cpp_var.begin()
                |cdef $tt item
                |while it != $input_cpp_var.end():
                |   item = $tt.__new__($tt)
                |   item.inst = new _$tt(deref(it))
                |   $output_py_var.append(item)
                |   inc(it)
                """, locals())
            return code

        else:
            code = Code().add("""
                |$output_py_var = []
                |cdef std_vector[$tt].iterator it = $input_cpp_var.begin()
                |cdef $tt item
                |while it != $input_cpp_var.end():
                |   $output_py_var.append(deref(it))
                |   inc(it)
                """, locals())
            return code



class StdStringConverter(TypeConverterBase):

    def get_base_types(self):
        return "std_string",

    def matches(self, cpp_type):
        return not cpp_type.is_ptr

    def matching_python_type(self, cpp_type):
        return "str"

    def input_conversion(self, cpp_type, argument_var, arg_num):
        code = ""
        call_as = "(<std_string>%s)" % argument_var
        cleanup = ""
        return code, call_as, cleanup

    def type_check_expression(self, cpp_type, argument_var):
        return "isinstance(%s, str)" % argument_var

    def output_conversion(self, cpp_type, input_cpp_var, output_py_var):
        return "%s = <std_string>%s" % (output_py_var, input_cpp_var)


def setup_converter_registry(names_of_classes_to_wrap):

    converter = ConverterRegistry()
    converter.register(NumberConverter())
    converter.register(CharPtrConverter())
    converter.register(StdStringConverter())
    converter.register(StdVectorConverter())
    for name in names_of_classes_to_wrap:
        converter.register(TypeToWrapConverter(name))

    converter.set_names_of_classes_to_wrap(names_of_classes_to_wrap)

    return converter
