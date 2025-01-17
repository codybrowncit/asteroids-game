1"""
Decompiler for Python3.2.
Decompile a module or a function using the decompile() function

>>> from unpyc3 import decompile
>>> def foo(x, y, z=3, *args):
...    global g
...    for i, j in zip(x, y):
...        if z == i + j or args[i] == j:
...            g = i, j
...            return
...    
>>> print(decompile(foo))

def foo(x, y, z=3, *args):
    global g
    for i, j in zip(x, y):
        if z == i + j or args[i] == j:
            g = i, j
            return
>>>
"""

__all__ = ['decompile']

# TODO:
# - Support for keyword-only arguments
# - Handle assert statements better
# - (Partly done) Nice spacing between function/class declarations

import dis
from array import array
from opcode import opname, opmap, HAVE_ARGUMENT, cmp_op
import imp
import inspect

# Masks for code object's co_flag attribute
VARARGS = 4
VARKEYWORDS = 8

# Put opcode names in the global namespace
for name, val in opmap.items():
    globals()[name] = val

# These opcodes will generate a statement. This is used in the first
# pass (in Code.find_else) to find which POP_JUMP_IF_* instructions
# are jumps to the else clause of an if statement
stmt_opcodes = {
    SETUP_LOOP, BREAK_LOOP, CONTINUE_LOOP,
    SETUP_FINALLY, END_FINALLY,
    SETUP_EXCEPT, POP_EXCEPT,
    SETUP_WITH,
    POP_BLOCK,
    STORE_FAST, DELETE_FAST,
    STORE_DEREF, DELETE_DEREF,
    STORE_GLOBAL, DELETE_GLOBAL,
    STORE_NAME, DELETE_NAME,
    STORE_ATTR, DELETE_ATTR,
    IMPORT_NAME, IMPORT_FROM,
    RETURN_VALUE, YIELD_VALUE,
    RAISE_VARARGS,
    POP_TOP,
}

# Conditional branching opcode that make up if statements and and/or
# expressions
pop_jump_if_opcodes = (POP_JUMP_IF_TRUE, POP_JUMP_IF_FALSE)

# These opcodes indicate that a pop_jump_if_x to the address just
# after them is an else-jump
else_jump_opcodes = (
    JUMP_FORWARD, RETURN_VALUE, JUMP_ABSOLUTE,
    SETUP_LOOP, RAISE_VARARGS
)

def read_code(stream): 
    # This helper is needed in order for the PEP 302 emulation to 
    # correctly handle compiled files
    # Note: stream must be opened in "rb" mode
    import marshal 
    magic = stream.read(4) 
    if magic != imp.get_magic(): 
        print("*** Warning: file has wrong magic number ***")
    stream.read(4) # Skip timestamp 
    return marshal.load(stream)

def dec_module(path):
    if path.endswith(".py"):
        path = imp.cache_from_source(path)
    elif not path.endswith(".pyc"):
        raise ValueError("path must point to a .py or .pyc file")
    stream = open(path, "rb")
    code_obj = read_code(stream)
    code = Code(code_obj)
    return code.get_suite(include_declarations=False, look_for_docstring=True)


def decompile(obj):
    """
    Decompile obj if it is a module object, a function or a
    code object. If obj is a string, it is assumed to be the path
    to a python module.
    """
    if isinstance(obj, str):
        return dec_module(obj)
    if inspect.iscode(obj):
        code = Code(obj)
        return code.get_suite()
    if inspect.isfunction(obj):
        code = Code(obj.__code__)
        defaults = obj.__defaults__
        kwdefaults = obj.__kwdefaults__
        return DefStatement(code, defaults, kwdefaults, obj.__closure__)
    elif inspect.ismodule(obj):
        return dec_module(obj.__file__)
    else:
        msg = "Object must be string, module, function or code object"
        raise TypeError(msg)

class Indent:
    def __init__(self, indent_level=0, indent_step=4):
        self.level = indent_level
        self.step = indent_step
    def write(self, pattern, *args, **kwargs):
        if args or kwargs:
            pattern = pattern.format(*args, **kwargs)
        return self.indent(pattern)
    def __add__(self, indent_increase):
        return type(self)(self.level + indent_increase, self.step)

class IndentPrint(Indent):
    def indent(self, string):
        print(" "*self.step*self.level + string)

class IndentString(Indent):
    def __init__(self, indent_level=0, indent_step=4, lines=None):
        Indent.__init__(self, indent_level, indent_step)
        if lines is None:
            self.lines = []
        else:
            self.lines = lines
    def __add__(self, indent_increase):
        return type(self)(self.level + indent_increase, self.step, self.lines)
    def sep(self):
        if not self.lines or self.lines[-1]:
            self.lines.append("")
    def indent(self, string):
        self.lines.append(" "*self.step*self.level + string)
    def __str__(self):
        return "\n".join(self.lines)

class Stack:
    def __init__(self):
        self._stack = []
        self._counts = {}
    def __bool__(self):
        return bool(self._stack)
    def __len__(self):
        return len(self._stack)
    def __contains__(self, val):
        return self.get_count(val) > 0
    def get_count(self, obj):
        return self._counts.get(id(obj), 0)
    def set_count(self, obj, val):
        if val:
            self._counts[id(obj)] = val
        else:
            del self._counts[id(obj)]
    def pop1(self):
        val = self._stack.pop()
        self.set_count(val, self.get_count(val) - 1)
        return val
    def pop(self, count=None):
        if count is None:
            return self.pop1()
        else:
            vals = [self.pop1() for i in range(count)]
            vals.reverse()
            return vals
    def push(self, *args):
        for val in args:
            self.set_count(val, self.get_count(val) + 1)
            self._stack.append(val)
    def peek(self, count=None):
        if count is None:
            return self._stack[-1]
        else:
            return self._stack[-count:]


def code_walker(code):
    l = len(code)
    code = array('B', code)
    i = 0
    while i < l:
        op = code[i]
        if op >= HAVE_ARGUMENT:
            yield i, (op, code[i+1] + (code[i+2] << 8))
            i += 3
        else:
            yield i, (op, None)
            i += 1

class Code:
    def __init__(self, code_obj, parent=None):
        self.code_obj = code_obj
        self.parent = parent
        self.derefnames = [PyName(v)
                           for v in code_obj.co_cellvars + code_obj.co_freevars]
        self.consts = list(map(PyConst, code_obj.co_consts))
        self.names = list(map(PyName, code_obj.co_names))
        self.varnames = list(map(PyName, code_obj.co_varnames))
        self.instr_seq = list(code_walker(code_obj.co_code))
        self.instr_map = {addr: i for i, (addr, _) in enumerate(self.instr_seq)}
        self.name = code_obj.co_name
        self.globals = []
        self.nonlocals = []
        self.find_else()
    def __getitem__(self, instr_index):
        if 0 <= instr_index < len(self.instr_seq):
            return Address(self, instr_index)
    def __iter__(self):
        for i in range(len(self.instr_seq)):
            yield Address(self, i)
    def show(self):
        for addr in self:
            print(addr)
    def address(self, addr):
        return self[self.instr_map[addr]]
    def iscellvar(self, i):
        return i < len(self.code_obj.co_cellvars)
    def find_else(self):
        jumps = {}
        last_jump = None
        for addr in self:
            opcode, arg = addr
            if opcode in pop_jump_if_opcodes:
                jump_addr = self.address(arg)
                if (jump_addr[-1].opcode in else_jump_opcodes
                    or jump_addr.opcode == FOR_ITER):
                    last_jump = addr
                    jumps[jump_addr] = addr
            elif opcode == JUMP_ABSOLUTE:
                # This case is to deal with some nested ifs such as:
                # if a:
                #     if b:
                #         f()
                #     elif c:
                #         g()
                jump_addr = self.address(arg)
                if jump_addr in jumps:
                    jumps[addr] = jumps[jump_addr]
            elif opcode in stmt_opcodes and last_jump is not None:
                # This opcode will generate a statement, so it means
                # that the last POP_JUMP_IF_x was an else-jump
                jumps[addr] = last_jump
        self.else_jumps = set(jumps.values())
    def get_suite(self, include_declarations=True, look_for_docstring=False):
        dec = SuiteDecompiler(self[0])
        dec.run()
        first_stmt = dec.suite and dec.suite[0]
        # Change __doc__ = "docstring" to "docstring"
        if look_for_docstring and isinstance(first_stmt, AssignStatement):
            chain = first_stmt.chain
            if len(chain) == 2 and str(chain[0]) == "__doc__":
                dec.suite[0] = DocString(first_stmt.chain[1].val)
        if include_declarations and (self.globals or self.nonlocals):
            suite = Suite()
            if self.globals:
                stmt = "global " + ", ".join(map(str, self.globals))
                suite.add_statement(SimpleStatement(stmt))
            if self.nonlocals:
                stmt = "nonlocal " + ", ".join(map(str, self.nonlocals))
                suite.add_statement(SimpleStatement(stmt))
            for stmt in dec.suite:
                suite.add_statement(stmt)
            return suite
        else:
            return dec.suite
    def declare_global(self, name):
        """
        Declare name as a global.  Called by STORE_GLOBAL and
        DELETE_GLOBAL
        """
        if name not in self.globals:
            self.globals.append(name)
    def ensure_global(self, name):
        """
        Declare name as global only if it is also a local variable
        name in one of the surrounding code objects.  This is called
        by LOAD_GLOBAL
        """
        parent = self.parent
        while parent:
            if name in parent.varnames:
                return self.declare_global(name)
            parent = parent.parent
    def declare_nonlocal(self, name):
        """
        Declare name as nonlocal.  Called by STORE_DEREF and
        DELETE_DEREF (but only when the name denotes a free variable,
        not a cell one).
        """
        if name not in self.nonlocals:
            self.nonlocals.append(name)


class Address:
    def __init__(self, code, instr_index):
        self.code = code
        self.index = instr_index
        self.addr, (self.opcode, self.arg) = code.instr_seq[instr_index]
    def __eq__(self, other):
        return (isinstance(other, type(self))
                and self.code == other.code and self.index == other.index)
    def __lt__(self, other):
        return other is None or (isinstance(other, type(self))
                 and self.code == other.code and self.index < other.index)
    def __str__(self):
        mark = "*" if self in self.code.else_jumps else " "
        return "{} {} {} {}".format(
            mark, self.addr,
            opname[self.opcode], self.arg or ""
        )
    def __add__(self, delta):
        return self.code.address(self.addr + delta)
    def __getitem__(self, index):
        return self.code[self.index + index]
    def __iter__(self):
        yield self.opcode
        yield self.arg
    def __hash__(self):
        return hash((self.code, self.index))
    def is_else_jump(self):
        return self in self.code.else_jumps
    def change_instr(self, opcode, arg=None):
        self.code.instr_seq[self.index] = (self.addr, (opcode, arg))
    def jump(self):
        opcode = self.opcode
        if opcode in dis.hasjrel:
            return self[1] + self.arg
        elif opcode in dis.hasjabs:
            return self.code.address(self.arg)


class PyExpr:
    def wrap(self, condition=True):
        if condition:
            return "({})".format(self)
        else:
            return str(self)
    def store(self, dec, dest):
        chain = dec.assignment_chain
        chain.append(dest)
        if self not in dec.stack:
            chain.append(self)
            dec.suite.add_statement(AssignStatement(chain))
            dec.assignment_chain = []
    def on_pop(self, dec):
        dec.write(str(self))

class PyConst(PyExpr):
    precedence = 100
    def __init__(self, val):
        self.val = val
    def __str__(self):
        return repr(self.val)
    def __iter__(self):
        return iter(self.val)
    def __eq__(self, other):
        return isinstance(other, PyConst) and self.val == other.val

class PyTuple(PyExpr):
    precedence = 0
    def __init__(self, values):
        self.values = values
    def __str__(self):
        if not self.values:
            return "()"
        valstr = [val.wrap(val.precedence <= self.precedence)
                  for val in self.values]
        if len(valstr) == 1:
            return valstr[0] + ","
        else:
            return ", ".join(valstr)
    def __iter__(self):
        return iter(self.values)

class PyList(PyExpr):
    precedence = 16
    def __init__(self, values):
        self.values = values
    def __str__(self):
        valstr = ", ".join(val.wrap(val.precedence <= 0)
                           for val in self.values)
        return "[{}]".format(valstr)
    def __iter__(self):
        return iter(self.values)
    
class PySet(PyExpr):
    precedence = 16
    def __init__(self, values):
        self.values = values
    def __str__(self):
        valstr = ", ".join(val.wrap(val.precedence <= 0)
                           for val in self.values)
        return "{{{}}}".format(valstr)
    def __iter__(self):
        return iter(self.values)

class PyDict(PyExpr):
    precedence = 16
    def __init__(self):
        self.items = []
    def set_item(self, key, val):
        self.items.append((key, val))
    def __str__(self):
        itemstr = ", ".join("{}: {}".format(*kv) for kv in self.items)
        return "{{{}}}".format(itemstr)

class PyName(PyExpr):
    precedence = 100
    def __init__(self, name):
        self.name = name
    def __str__(self):
        return self.name
    def __eq__(self, other):
        return isinstance(other, type(self)) and self.name == other.name

class PyUnaryOp(PyExpr):
    def __init__(self, operand):
        self.operand = operand
    def __str__(self):
        opstr = self.operand.wrap(self.operand.precedence < self.precedence)
        return self.pattern.format(opstr)
    @classmethod
    def instr(cls, stack):
        stack.push(cls(stack.pop()))

class PyBinaryOp(PyExpr):
    def __init__(self, left, right):
        self.left = left
        self.right = right
    def wrap_left(self):
        return self.left.wrap(self.left.precedence < self.precedence)
    def wrap_right(self):
        return self.right.wrap(self.right.precedence <= self.precedence)
    def __str__(self):
        return self.pattern.format(self.wrap_left(), self.wrap_right())
    @classmethod
    def instr(cls, stack):
        right = stack.pop()
        left = stack.pop()
        stack.push(cls(left, right))

class PySubscript(PyBinaryOp):
    precedence = 15
    pattern = "{}[{}]"
    def wrap_right(self):
        return str(self.right)

class PySlice(PyExpr):
    precedence = 1
    def __init__(self, args):
        assert len(args) in (2, 3)
        if len(args) == 2:
            self.start, self.stop = args
            self.step = None
        else:
            self.start, self.stop, self.step = args
        if self.start == PyConst(None):
            self.start = ""
        if self.stop == PyConst(None):
            self.stop = ""
    def __str__(self):
        if self.step is None:
            return "{}:{}".format(self.start, self.stop)
        else:
            return "{}:{}:{}".format(self.start, self.stop, self.step)

class PyCompare(PyExpr):
    precedence = 6
    def __init__(self, complist):
        self.complist = complist
    def __str__(self):
        return " ".join(x if i%2 else x.wrap(x.precedence <= 0)
                        for i, x in enumerate(self.complist))
    def extends(self, other):
        if not isinstance(other, PyCompare):
            return False
        else:
            return self.complist[0] == other.complist[-1]
    def chain(self, other):
        return PyCompare(self.complist + other.complist[1:])

class PyBooleanAnd(PyBinaryOp):
    precedence = 4
    pattern = "{} and {}"

class PyBooleanOr(PyBinaryOp):
    precedence = 3
    pattern = "{} or {}"

class PyIfElse(PyExpr):
    precedence = 2
    def __init__(self, cond, true_expr, false_expr):
        self.cond = cond
        self.true_expr = true_expr
        self.false_expr = false_expr
    def __str__(self):
        p = self.precedence
        cond_str = self.cond.wrap(self.cond.precedence <= p)
        true_str = self.true_expr.wrap(self.cond.precedence <= p)
        false_str = self.false_expr.wrap(self.cond.precedence < p)
        return "{} if {} else {}".format(true_str, cond_str, false_str)

class PyAttribute(PyExpr):
    precedence = 15
    def __init__(self, expr, attrname):
        self.expr = expr
        self.attrname = attrname
    def __str__(self):
        expr_str = self.expr.wrap(self.expr.precedence < self.precedence)
        return "{}.{}".format(expr_str, self.attrname)

class PyCallFunction(PyExpr):
    precedence = 15
    def __init__(self, func, args, kwargs, varargs=None, varkw=None):
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self.varargs = varargs
        self.varkw = varkw
    def __str__(self):
        funcstr = self.func.wrap(self.func.precedence < self.precedence)
        if len(self.args) == 1 and not (self.kwargs or self.varargs
                                         or self.varkw):
            arg = self.args[0]
            if isinstance(arg, PyGenExpr):
                # Only one pair of brackets arount a single arg genexpr
                return "{}{}".format(funcstr, arg)
        args = [x.wrap(x.precedence <= 0) for x in self.args]
        args.extend("{}={}".format(k.val, v.wrap(v.precedence <= 0))
                    for k, v in self.kwargs)
        if self.varargs is not None:
            args.append("*{}".format(self.varargs))
        if self.varkw is not None:
            args.append("**{}".format(self.varkw))
        return "{}({})".format(funcstr, ", ".join(args))

class FunctionDefinition:
    def __init__(self, code, defaults, kwdefaults, closure):
        self.code = code
        self.defaults = defaults
        self.kwdefaults = kwdefaults
        self.closure = closure
    def getparams(self):
        code_obj = self.code.code_obj
        l = code_obj.co_argcount
        params = list(code_obj.co_varnames[:l])
        if self.defaults:
            for i, arg in enumerate(reversed(self.defaults)):
                params[-i - 1] = "{}={}".format(params[-i - 1], arg)
        kwcount = code_obj.co_kwonlyargcount
        kwparams = []
        if kwcount:
            for i in range(kwcount):
                name = code_obj.co_varnames[l + i]
                if name in self.kwdefaults:
                    kwparams.append("{}={}".format(name, self.kwdefaults[name]))
                else:
                    kwparams.append(name)
            l += kwcount
        if code_obj.co_flags & VARARGS:
            params.append("*" + code_obj.co_varnames[l])
            l += 1
        elif kwparams:
            params.append("*")
        params.extend(kwparams)
        if code_obj.co_flags & VARKEYWORDS:
            params.append("**" + code_obj.co_varnames[l])
        return params

class PyLambda(PyExpr, FunctionDefinition):
    precedence = 1
    def __str__(self):
        suite = self.code.get_suite()
        params = ", ".join(self.getparams())
        expr = suite[0].val[len("return "):]
        return "lambda {}: {}".format(params, expr)

class PyComp(PyExpr):
    """
    Abstraction for list, set, dict comprehensions and generator expressions
    """
    precedence = 16
    def __init__(self, code, defaults, kwdefaults, closure):
        assert not defaults and not kwdefaults
        self.code = code
        code[0].change_instr(NOP)
        last_i = len(code.instr_seq) - 1
        code[last_i].change_instr(NOP)
    def set_iterable(self, iterable):
        self.code.varnames[0] = iterable
    def __str__(self):
        suite = self.code.get_suite()
        return self.pattern.format(suite.gen_display())

class PyListComp(PyComp):
    pattern = "[{}]"

class PySetComp(PyComp):
    pattern = "{{{}}}"

class PyKeyValue(PyBinaryOp):
    """This is only to create dict comprehensions"""
    precedence = 1
    pattern = "{}: {}"

class PyDictComp(PyComp):
    pattern = "{{{}}}"

class PyGenExpr(PyComp):
    precedence = 16
    pattern = "({})"
    def __init__(self, code, defaults, kwdefaults, closure):
        self.code = code

class PyYield(PyExpr):
    precedence = 1
    def __init__(self, value):
        self.value = value
    def __str__(self):
        return "yield {}".format(self.value)

class PyStarred(PyExpr):
    """Used in unpacking assigments"""
    precedence = 15
    def __init__(self, expr):
        self.expr = expr
    def __str__(self):
        es = self.expr.wrap(self.expr.precedence < self.precedence)
        return "*{}".format(es)


code_map = {
    '<lambda>': PyLambda,
    '<listcomp>': PyListComp,
    '<setcomp>': PySetComp,
    '<dictcomp>': PyDictComp,
    '<genexpr>': PyGenExpr,
}

unary_ops = [
    ('UNARY_POSITIVE', 'Positive', '+{}', 13),
    ('UNARY_NEGATIVE', 'Negative', '-{}', 13),
    ('UNARY_NOT', 'Not', 'not {}', 5),
    ('UNARY_INVERT', 'Invert', '~{}', 13),
]

binary_ops = [
    ('POWER', 'Power', '{}**{}', 14, '{} **= {}'),
    ('MULTIPLY', 'Multiply', '{}*{}', 12, '{} *= {}'),
    ('FLOOR_DIVIDE', 'FloorDivide', '{}//{}', 12, '{} //= {}'),
    ('TRUE_DIVIDE', 'TrueDivide', '{}/{}', 12, '{} /= {}'),
    ('MODULO', 'Modulo', '{} % {}', 12, '{} %= {}'),
    ('ADD', 'Add', '{} + {}', 11, '{} += {}'),
    ('SUBTRACT', 'Subtract', '{} - {}', 11, '{} -= {}'),
    ('SUBSCR', 'Subscript', '{}[{}]', 15, None),
    ('LSHIFT', 'LeftShift', '{} << {}', 10, '{} <<= {}'),
    ('RSHIFT', 'RightShift', '{} >> {}', 10, '{} >>= {}'),
    ('AND', 'And', '{} & {}', 9, '{} &= {}'),
    ('XOR', 'Xor', '{} ^ {}', 8, '{} ^= {}'),
    ('OR', 'Or', '{} | {}', 7, '{} |= {}'),
]


class PyStatement:
    def __str__(self):
        istr = IndentString()
        self.display(istr)
        return str(istr)

class DocString(PyStatement):
    def __init__(self, string):
        self.string = string
    def display(self, indent):
        if '\n' not in self.string:
            indent.write(repr(self.string))
        else:
            if "'''" not in self.string:
                fence = "'''"
            elif '"""' not in self.string:
                fence = '"""'
            else:
                raise NotImplemented
            lines = self.string.split('\n')
            text = '\n'.join(l.encode('unicode_escape').decode()
                             for l in lines)
            docstring = "{0}{1}{0}".format(fence, text)
            indent.write(docstring)

class AssignStatement(PyStatement):
    def __init__(self, chain):
        self.chain = chain
    def display(self, indent):
        indent.write(" = ".join(map(str, self.chain)))

class InPlaceOp(PyStatement):
    def __init__(self, left, right):
        self.right = right
        self.left = left
    def store(self, dec, dest):
        # assert dest is self.left
        dec.suite.add_statement(self)
    def display(self, indent):
        indent.write(self.pattern, self.left, self.right)

class Unpack:
    def __init__(self, val, length, star_index=None):
        self.val = val
        self.length = length
        self.star_index = star_index
        self.dests = [] 
    def store(self, dec, dest):
        if len(self.dests) == self.star_index:
            dest = PyStarred(dest)
        self.dests.append(dest)
        if len(self.dests) == self.length:
            dec.stack.push(self.val)
            dec.store(PyTuple(self.dests))

class ImportStatement(PyStatement):
    def __init__(self, name, level, fromlist):
        self.name = name
        self.level = level
        self.fromlist = fromlist
        self.aslist = []
    def store(self, dec, dest):
        self.alias = dest
        dec.suite.add_statement(self)
    def on_pop(self, dec):
        dec.suite.add_statement(self)
    def display(self, indent):
        if self.fromlist == PyConst(None):
            name = self.name.name
            alias = self.alias.name
            if name == alias or name.startswith(alias + "."):
                indent.write("import {}", name)
            else:
                indent.write("import {} as {}", name, alias)
        elif self.fromlist == PyConst(('*',)):
            indent.write("from {} import *", self.name.name)
        else:
            names = []
            for name, alias in zip(self.fromlist, self.aslist):
                if name == alias:
                    names.append(name)
                else:
                    names.append("{} as {}".format(name, alias))
            indent.write("from {} import {}", self.name, ", ".join(names))
            
class ImportFrom:
    def __init__(self, name):
        self.name = name
    def store(self, dec, dest):
        imp = dec.stack.peek()
        assert isinstance(imp, ImportStatement)
        imp.aslist.append(dest.name)


class SimpleStatement(PyStatement):
    def __init__(self, val):
        assert val is not None
        self.val = val
    def display(self, indent):
        indent.write(self.val)
    def gen_display(self, seq=()):
        return " ".join((self.val,) + seq)

class IfStatement(PyStatement):
    def __init__(self, cond, true_suite, false_suite):
        self.cond = cond
        self.true_suite = true_suite
        self.false_suite = false_suite
    def display(self, indent, is_elif=False):
        ptn = "elif {}:" if is_elif else "if {}:"
        indent.write(ptn, self.cond)
        self.true_suite.display(indent + 1)
        if not self.false_suite:
            return
        if len(self.false_suite) == 1:
            stmt = self.false_suite[0]
            if isinstance(stmt, IfStatement):
                stmt.display(indent, is_elif=True)
                return
        indent.write("else:")
        self.false_suite.display(indent + 1)
    def gen_display(self, seq=()):
        assert not self.false_suite
        s = "if {}".format(self.cond)
        return self.true_suite.gen_display(seq + (s,))

class ForStatement(PyStatement):
    def __init__(self, iterable):
        self.iterable = iterable
    def store(self, dec, dest):
        self.dest = dest
    def display(self, indent):
        indent.write("for {} in {}:", self.dest, self.iterable)
        self.body.display(indent + 1)
    def gen_display(self, seq=()):
        s = "for {} in {}".format(self.dest, self.iterable)
        return self.body.gen_display(seq + (s,))

class WhileStatement(PyStatement):
    def __init__(self, cond, body):
        self.cond = cond
        self.body = body
    def display(self, indent):
        indent.write("while {}:", self.cond)
        self.body.display(indent + 1)

class DecorableStatement(PyStatement):
    def __init__(self):
        self.decorators = []
    def display(self, indent):
        indent.sep()
        for f in reversed(self.decorators):
            indent.write("@{}", f)
        self.display_undecorated(indent)
        indent.sep()
    def decorate(self, f):
        self.decorators.append(f)

class DefStatement(FunctionDefinition, DecorableStatement):
    def __init__(self, code, defaults, kwdefaults, closure):
        FunctionDefinition.__init__(self, code, defaults, kwdefaults, closure)
        DecorableStatement.__init__(self)
    def display_undecorated(self, indent):
        paramlist = ", ".join(self.getparams())
        indent.write("def {}({}):", self.code.name, paramlist)
        # Assume that co_consts starts with None unless the function
        # has a docstring, in which case it starts with the docstring
        if self.code.consts[0] != PyConst(None):
            docstring = self.code.consts[0].val
            DocString(docstring).display(indent + 1)
        self.code.get_suite().display(indent + 1)
    def store(self, dec, dest):
        self.name = dest
        dec.suite.add_statement(self)

class TryStatement(PyStatement):
    def __init__(self, try_suite):
        self.try_suite = try_suite
        self.except_clauses = []
    def add_except_clause(self, type, suite):
        self.except_clauses.append([type, None, suite])
    def store(self, dec, dest):
        self.except_clauses[-1][1] = dest
    def display(self, indent):
        indent.write("try:")
        self.try_suite.display(indent + 1)
        for type, name, suite in self.except_clauses:
            if type is None:
                indent.write("except:")
            elif name is None:
                indent.write("except {}:", type)
            else:
                indent.write("except {} as {}:", type, name)
            suite.display(indent + 1)

class FinallyStatement(PyStatement):
    def __init__(self, try_suite, finally_suite):
        self.try_suite = try_suite
        self.finally_suite = finally_suite
    def display(self, indent):
        # Wrap the try suite in a TryStatement if necessary
        try_stmt = None
        if len(self.try_suite) == 1:
            try_stmt = self.try_suite[0]
            if not isinstance(try_stmt, TryStatement):
                try_stmt = None
        if try_stmt is None:
            try_stmt = TryStatement(self.try_suite)
        try_stmt.display(indent)
        indent.write("finally:")
        self.finally_suite.display(indent + 1)

class WithStatement(PyStatement):
    def __init__(self, with_expr):
        self.with_expr = with_expr
        self.with_name = None
    def store(self, dec, dest):
        self.with_name = dest
    def display(self, indent, args=None):
        # args to take care of nested withs:
        # with x as t:
        #     with y as u:
        #         <suite>
        # --->
        # with x as t, y as u:
        #     <suite>
        if args is None:
            args = []
        if self.with_name is None:
            args.append(str(self.with_expr))
        else:
            args.append("{} as {}".format(self.with_expr, self.with_name))
        if len(self.suite) == 1 and isinstance(self.suite[0], WithStatement):
            self.suite[0].display(indent, args)
        else:
            indent.write("with {}:", ", ".join(args))
            self.suite.display(indent + 1)

class ClassStatement(DecorableStatement):
    def __init__(self, func, name, parents, kwargs):
        DecorableStatement.__init__(self)
        self.func = func
        self.parents = parents
        self.kwargs = kwargs
    def store(self, dec, dest):
        self.name = dest
        dec.suite.add_statement(self)
    def display_undecorated(self, indent):
        if self.parents or self.kwargs:
            args = [str(x) for x in self.parents]
            kwargs = ["{}={}".format(k.val, v) for k, v in self.kwargs]
            all_args = ", ".join(args + kwargs)
            indent.write("class {}({}):", self.name, all_args)
        else:
            indent.write("class {}:", self.name)
        suite = self.func.code.get_suite(look_for_docstring=True)
        if suite:
            # TODO: find out why sometimes the class suite ends with
            # "return __class__"
            last_stmt = suite[-1]
            if isinstance(last_stmt, SimpleStatement):
                if last_stmt.val.startswith("return "):
                    suite.statements.pop()
        suite.display(indent + 1)

class Suite:
    def __init__(self):
        self.statements = []
    def __bool__(self):
        return bool(self.statements)
    def __len__(self):
        return len(self.statements)
    def __getitem__(self, i):
        return self.statements[i]
    def __setitem__(self, i, val):
        self.statements[i] = val
    def __str__(self):
        istr = IndentString()
        self.display(istr)
        return str(istr)
    def display(self, indent):
        if self.statements:
            for stmt in self.statements:
                stmt.display(indent)
        else:
            indent.write("pass")
    def gen_display(self, seq=()):
        assert len(self) == 1
        return self[0].gen_display(seq)
    def add_statement(self, stmt):
        self.statements.append(stmt)


class SuiteDecompiler:

    # An instruction handler can return this to indicate to the run()
    # function that it should return immediately
    END_NOW = object()

    # This is put on the stack by LOAD_BUILD_CLASS
    BUILD_CLASS = object()
    
    def __init__(self, start_addr, end_addr=None, stack=None):
        self.start_addr = start_addr
        self.end_addr = end_addr
        self.code = start_addr.code
        self.stack = Stack() if stack is None else stack
        self.suite = Suite()
        self.assignment_chain = []
        self.popjump_stack = []

    def push_popjump(self, jtruthiness, jaddr, jcond):
        stack = self.popjump_stack
        if jaddr and jaddr[-1].is_else_jump():
            # Increase jaddr to the 'else' address if it jumps to the 'then'
            jaddr = jaddr[-1].jump()
        while stack:
            truthiness, addr, cond = stack[-1]
            if jaddr < addr or jaddr == addr:
                break
            stack.pop()
            obj_maker = PyBooleanOr if truthiness else PyBooleanAnd
            if isinstance(jcond, obj_maker):
                # Use associativity of 'and' and 'or' to minimise the
                # number of parentheses
                jcond = obj_maker(obj_maker(cond, jcond.left), jcond.right)
            else:
                jcond = obj_maker(cond, jcond)
        stack.append((jtruthiness, jaddr, jcond))
                
    def pop_popjump(self):
        truthiness, addr, cond = self.popjump_stack.pop()
        return cond
    
    def run(self):
        addr, end_addr = self.start_addr, self.end_addr
        while addr and addr < end_addr:
            opcode, arg = addr
            method = getattr(self, opname[opcode])
            if arg is None:
                new_addr = method(addr)
            else:
                new_addr = method(addr, arg)
            if new_addr is self.END_NOW:
                break
            elif new_addr is None:
                new_addr = addr[1]
            addr = new_addr
        return addr
    
    def write(self, template, *args):
        def fmt(x):
            if isinstance(x, int):
                return self.stack.getval(x)
            else:
                return x
        if args:
            line = template.format(*map(fmt, args))
        else:
            line = template
        self.suite.add_statement(SimpleStatement(line))
    
    def store(self, dest):
        val = self.stack.pop()
        val.store(self, dest)

    #
    # All opcode methods in CAPS below.
    #

    def SETUP_LOOP(self, addr, delta):
        pass

    def BREAK_LOOP(self, addr):
        self.write("break")

    def CONTINUE_LOOP(self, addr):
        self.write("continue")
    
    def SETUP_FINALLY(self, addr, delta):
        start_finally = addr.jump()
        d_try = SuiteDecompiler(addr[1], start_finally)
        d_try.run()
        d_finally = SuiteDecompiler(start_finally)
        end_finally = d_finally.run()
        self.suite.add_statement(FinallyStatement(d_try.suite, d_finally.suite))
        return end_finally[1]

    def END_FINALLY(self, addr):
        return self.END_NOW
    
    def SETUP_EXCEPT(self, addr, delta):
        start_except = addr.jump()
        end_try = start_except[-1]
        d_try = SuiteDecompiler(addr[1], start_except[-1])
        d_try.run()
        assert end_try.opcode == JUMP_FORWARD
        end_addr = end_try[1] + end_try.arg
        stmt = TryStatement(d_try.suite)
        while start_except.opcode != END_FINALLY:
            if start_except.opcode == DUP_TOP:
                # There's a new except clause
                d_except = SuiteDecompiler(start_except[1])
                d_except.stack.push(stmt)
                d_except.run()
                start_except = stmt.next_start_except
            elif start_except.opcode == POP_TOP:
                # It's a bare except clause - it starts:
                # POP_TOP
                # POP_TOP
                # POP_TOP
                # <except stuff>
                # POP_EXCEPT
                d_except = SuiteDecompiler(start_except[3])
                end_except = d_except.run()
                stmt.add_except_clause(None, d_except.suite)
                start_except = end_except[2]
                assert start_except.opcode == END_FINALLY
        self.suite.add_statement(stmt)
        return start_except[1]

    def SETUP_WITH(self, addr, delta):
        end_with = addr.jump()
        with_stmt = WithStatement(self.stack.pop())
        d_with = SuiteDecompiler(addr[1], end_with)
        d_with.stack.push(with_stmt)
        d_with.run()
        with_stmt.suite = d_with.suite
        self.suite.add_statement(with_stmt)
        assert end_with.opcode == WITH_CLEANUP
        assert end_with[1].opcode == END_FINALLY
        return end_with[2]
    
    def POP_BLOCK(self, addr):
        # print("** POP BLOCK:", addr)
        pass

    def POP_EXCEPT(self, addr):
        # print("** POP EXCEPT:", addr)
        return self.END_NOW
    
    def NOP(self, addr):
        return

    def COMPARE_OP(self, addr, opname):
        left, right = self.stack.pop(2)
        if opname != 10: # 10 is exception match
            self.stack.push(PyCompare([left, cmp_op[opname], right]))
        else:
            # It's an exception match
            # left is a TryStatement
            # right is the exception type to be matched
            # It goes:
            # COMPARE_OP 10
            # POP_JUMP_IF_FALSE <next except>
            # POP_TOP
            # POP_TOP or STORE_FAST (if the match is named)
            # POP_TOP
            # SETUP_FINALLY if the match was named
            assert addr[1].opcode == POP_JUMP_IF_FALSE
            left.next_start_except = addr[1].jump()
            assert addr[2].opcode == POP_TOP
            assert addr[4].opcode == POP_TOP
            if addr[5].opcode == SETUP_FINALLY:
                except_start = addr[6]
                except_end = addr[5].jump()
            else:
                except_start = addr[5]
                except_end = left.next_start_except[-1]
            d_body = SuiteDecompiler(except_start, except_end)
            d_body.run()
            left.add_except_clause(right, d_body.suite)
            if addr[3].opcode != POP_TOP:
                # The exception is named
                d_exc_name = SuiteDecompiler(addr[3], addr[4])
                d_exc_name.stack.push(left)
                # This will store the name in left:
                d_exc_name.run()
            # We're done with this except clause
            return self.END_NOW

    #
    # Stack manipulation
    #
    
    def POP_TOP(self, addr):
        self.stack.pop().on_pop(self)

    def ROT_TWO(self, addr):
        tos1, tos = self.stack.pop(2)
        self.stack.push(tos, tos1)

    def ROT_THREE(self, addr):
        tos2, tos1, tos = self.stack.pop(3)
        self.stack.push(tos, tos2, tos1)

    def DUP_TOP(self, addr):
        self.stack.push(self.stack.peek())
    
    def DUP_TOP_TWO(self, addr):
        self.stack.push(*self.stack.peek(2))

    #
    # LOAD / STORE / DELETE
    #
    
    # FAST
    
    def LOAD_FAST(self, addr, var_num):
        name = self.code.varnames[var_num]
        self.stack.push(name)

    def STORE_FAST(self, addr, var_num):
        name = self.code.varnames[var_num]
        self.store(name)

    def DELETE_FAST(self, addr, var_num):
        name = self.code.varnames[var_num]
        self.write("del {}", name)

    # DEREF

    def LOAD_DEREF(self, addr, i):
        name = self.code.derefnames[i]
        self.stack.push(name)

    def STORE_DEREF(self, addr, i):
        name = self.code.derefnames[i]
        if not self.code.iscellvar(i):
            self.code.declare_nonlocal(name)
        self.store(name)

    def DELETE_DEREF(self, addr, i):
        name = self.code.getderefname(i)
        if not self.code.iscellvar(i):
            self.code.declare_nonlocal(name)
        self.write("del {}", name)
    
    # GLOBAL
    
    def LOAD_GLOBAL(self, addr, namei):
        name = self.code.names[namei]
        self.code.ensure_global(name)
        self.stack.push(name)

    def STORE_GLOBAL(self, addr, namei):
        name = self.code.names[namei]
        self.code.declare_global(name)
        self.store(name)

    def DELETE_GLOBAL(self, addr, namei):
        name = self.code.names[namei]
        self.declare_global(name)
        self.write("del {}", name)

    # NAME
    
    def LOAD_NAME(self, addr, namei):
        name = self.code.names[namei]
        self.stack.push(name)

    def STORE_NAME(self, addr, namei):
        name = self.code.names[namei]
        self.store(name)

    def DELETE_NAME(self, addr, namei):
        name = self.code.names[namei]
        self.write("del {}", name)

    # ATTR
    
    def LOAD_ATTR(self, addr, namei):
        expr = self.stack.pop()
        attrname = self.code.names[namei]
        self.stack.push(PyAttribute(expr, attrname))
    
    def STORE_ATTR(self, addr, namei):
        expr = self.stack.pop()
        attrname = self.code.names[namei]
        self.store(PyAttribute(expr, attrname))

    def DELETE_ATTR(self, addr, namei):
        expr = self.stack.pop()
        attrname = self.code.names[namei]
        self.write("del {}.{}", expr, attrname)

    # SUBSCR
    
    def STORE_SUBSCR(self, addr):
        expr, sub = self.stack.pop(2)
        self.store(PySubscript(expr, sub))

    def DELETE_SUBSCR(self, addr):
        expr, sub = self.stack.pop(2)
        self.write("del {}[{}]", expr, sub)
    
    # CONST
    
    def LOAD_CONST(self, addr, consti):
        const = self.code.consts[consti]
        self.stack.push(const)
    
    #
    # Import statements
    #
    
    def IMPORT_NAME(self, addr, namei):
        name = self.code.names[namei]
        level, fromlist = self.stack.pop(2)
        self.stack.push(ImportStatement(name, level, fromlist))
    
    def IMPORT_FROM(self, addr, namei):
        name = self.code.names[namei]
        self.stack.push(ImportFrom(name))

    def IMPORT_STAR(self, addr):
        self.POP_TOP(addr)
    
    #
    # Function call
    #

    def STORE_LOCALS(self, addr):
        self.stack.pop()
        return addr[3]
    
    def LOAD_BUILD_CLASS(self, addr):
        self.stack.push(self.BUILD_CLASS)

    def RETURN_VALUE(self, addr):
        value = self.stack.pop()
        if isinstance(value, PyConst) and value.val is None:
            if addr[1] is not None:
                self.write("return")
            return
        self.write("return {}", value)

    def YIELD_VALUE(self, addr):
        if self.code.name == '<genexpr>':
            return
        value = self.stack.pop()
        self.stack.push(PyYield(value))
    
    def CALL_FUNCTION(self, addr, argc, have_var=False, have_kw=False):
        kw_argc = argc >> 8
        pos_argc = argc & 0xFF
        varkw = self.stack.pop() if have_kw else None
        varargs = self.stack.pop() if have_var else None
        kwargs_iter = iter(self.stack.pop(2*kw_argc))
        kwargs = list(zip(kwargs_iter, kwargs_iter))
        posargs = self.stack.pop(pos_argc)
        func = self.stack.pop()
        if func is self.BUILD_CLASS:
            # It's a class construction
            # TODO: check the assert statement below is correct
            assert not (have_var or have_kw)
            func, name, *parents  = posargs
            self.stack.push(ClassStatement(func, name, parents, kwargs))
        elif isinstance(func, PyComp):
            # It's a list/set/dict comprehension or generator expression
            assert not (have_var or have_kw)
            assert len(posargs) == 1 and not kwargs
            func.set_iterable(posargs[0])
            self.stack.push(func)
        elif posargs and isinstance(posargs[0], DecorableStatement):
            # It's a decorator for a def/class statement
            assert len(posargs) == 1 and not kwargs
            defn = posargs[0]
            defn.decorate(func)
            self.stack.push(defn)
        else:
            # It's none of the above, so it must be a normal function call
            func_call = PyCallFunction(func, posargs, kwargs, varargs, varkw)
            self.stack.push(func_call)

    def CALL_FUNCTION_VAR(self, addr, argc):
        self.CALL_FUNCTION(addr, argc, have_var=True)
    
    def CALL_FUNCTION_KW(self, addr, argc):
        self.CALL_FUNCTION(addr, argc, have_kw=True)
    
    def CALL_FUNCTION_VAR_KW(self, addr, argc):
        self.CALL_FUNCTION(addr, argc, have_var=True, have_kw=True)
        
    # a, b, ... = ...
    
    def UNPACK_SEQUENCE(self, addr, count):
        unpack = Unpack(self.stack.pop(), count)
        for i in range(count):
            self.stack.push(unpack)

    def UNPACK_EX(self, addr, counts):
        rcount = counts >> 8
        lcount = counts & 0xFF
        count = lcount + rcount + 1
        unpack = Unpack(self.stack.pop(), count, lcount)
        for i in range(count):
            self.stack.push(unpack)
    
    # special case: x, y = z, t

    def ROT_TWO(self, addr):
        val = PyTuple(self.stack.pop(2))
        unpack = Unpack(val, 2)
        self.stack.push(unpack)
        self.stack.push(unpack)

    # Build operations

    def BUILD_SLICE(self, addr, argc):
        assert argc in (2, 3)
        self.stack.push(PySlice(self.stack.pop(argc)))
    
    def BUILD_TUPLE(self, addr, count):
        values = [self.stack.pop() for i in range(count)]
        values.reverse()
        self.stack.push(PyTuple(values))
    
    def BUILD_LIST(self, addr, count):
        values = [self.stack.pop() for i in range(count)]
        values.reverse()
        self.stack.push(PyList(values))

    def BUILD_SET(self, addr, count):
        values = [self.stack.pop() for i in range(count)]
        values.reverse()
        self.stack.push(PySet(values))

    def BUILD_MAP(self, addr, count):
        self.stack.push(PyDict())

    def STORE_MAP(self, addr):
        v, k = self.stack.pop(2)
        d = self.stack.peek()
        d.set_item(k, v)

    # Comprehension operations - just create an expression statement

    def LIST_APPEND(self, addr, i):
        self.POP_TOP(addr)

    def SET_ADD(self, addr, i):
        self.POP_TOP(addr)

    def MAP_ADD(self, addr, i):
        value, key = self.stack.pop(2)
        self.stack.push(PyKeyValue(key, value))
        self.POP_TOP(addr)
    
    # and operator

    def JUMP_IF_FALSE_OR_POP(self, addr, target):
        end_addr = addr.jump()
        self.push_popjump(True, end_addr, self.stack.pop())
        left = self.pop_popjump()
        if end_addr.opcode == ROT_TWO:
            opc, arg = end_addr[-1]
            if opc == JUMP_FORWARD and arg == 2:
                end_addr = end_addr[2]
        d = SuiteDecompiler(addr[1], end_addr, self.stack)
        d.run()
        right = self.stack.pop()
        if isinstance(right, PyCompare) and right.extends(left):
            py_and = left.chain(right)
        else:
            py_and = PyBooleanAnd(left, right)
        self.stack.push(py_and)
        return end_addr

    # This appears when there are chained comparisons, e.g. 1 <= x < 10
    
    def JUMP_FORWARD(self, addr, delta):
        # print("*** JUMP FORWARD", addr)
        ## if delta == 2 and addr[1].opcode == ROT_TWO and addr[2].opcode == POP_TOP:
        ##     # We're in the special case of chained comparisons
        ##     return addr[3]
        ## else:
        ##     # I'm hoping its an unused JUMP in an if-else statement
        ##     return addr[1]
        return addr.jump()
    
    # or operator
    
    def JUMP_IF_TRUE_OR_POP(self, addr, target):
        end_addr = addr.jump()
        self.push_popjump(True, end_addr, self.stack.pop())
        left = self.pop_popjump()
        d = SuiteDecompiler(addr[1], end_addr, self.stack)
        d.run()
        right = self.stack.pop()
        self.stack.push(PyBooleanOr(left, right))
        return end_addr

    #
    # If-else statements/expressions and related structures
    #

    def POP_JUMP_IF(self, addr, target, truthiness):
        jump_addr = addr.jump()
        if jump_addr.opcode == FOR_ITER:
            # We are in a for-loop with nothing after the if-suite
            # But take care: for-loops in generator expression do
            # not end in POP_BLOCK, hence the test below.
            jump_addr = jump_addr.jump()
            if jump_addr.opcode == POP_BLOCK:
                jump_addr = jump_addr[-1]
        elif jump_addr[-1].opcode == SETUP_LOOP:
            # We are in a while-loop with nothing after the if-suite
            jump_addr = jump_addr[-1].jump()[-2]
        cond = self.stack.pop()
        if not addr.is_else_jump():
            self.push_popjump(truthiness, jump_addr, cond)
            return
        # Increase jump_addr to pop all previous jumps
        self.push_popjump(truthiness, jump_addr[1], cond)
        cond = self.pop_popjump()
        end_true = jump_addr[-1]
        if truthiness:
            cond = PyNot(cond)
        # - If the true clause ends in return, make sure it's included
        # - If the true clause ends in RAISE_VARARGS, then it's an
        # assert statement. For now I just write it as a raise within
        # an if (see below)
        if end_true.opcode in (RETURN_VALUE, RAISE_VARARGS):
            # TODO: change
            #     if cond: raise AssertionError(x)
            # to
            #     assert cond, x
            d_true = SuiteDecompiler(addr[1], end_true[1])
            d_true.run()
            self.suite.add_statement(IfStatement(cond, d_true.suite, Suite()))
            return jump_addr
        d_true = SuiteDecompiler(addr[1], end_true)            
        d_true.run()
        if jump_addr.opcode == POP_BLOCK:
            # It's a while loop
            stmt = WhileStatement(cond, d_true.suite)
            self.suite.add_statement(stmt)
            return jump_addr[1]
        # It's an if-else (expression or statement)
        if end_true.opcode == JUMP_FORWARD:
            end_false = end_true.jump()
        elif end_true.opcode == JUMP_ABSOLUTE:
            end_false = end_true.jump()
            if end_false.opcode == FOR_ITER:
                # We are in a for-loop with nothing after the else-suite
                end_false = end_false.jump()[-1]
            elif end_false[-1].opcode == SETUP_LOOP:
                # We are in a while-loop with nothing after the else-suite
                end_false = end_false[-1].jump()[-2]
        elif end_true.opcode == RETURN_VALUE:
            # find the next RETURN_VALUE
            end_false = jump_addr
            while end_false.opcode != RETURN_VALUE:
                end_false = end_false[1]
            end_false = end_false[1]
        else:
            raise Unknown
        d_false = SuiteDecompiler(jump_addr, end_false)
        d_false.run()
        if not (d_true.stack or d_false.stack):
            stmt = IfStatement(cond, d_true.suite, d_false.suite)
            self.suite.add_statement(stmt)
        else:
            assert len(d_true.stack) == len(d_false.stack) == 1
            assert not (d_true.suite or d_false.suite)
            true_expr = d_true.stack.pop()
            false_expr = d_false.stack.pop()
            self.stack.push(PyIfElse(cond, true_expr, false_expr))
        return end_false or self.END_NOW

    def POP_JUMP_IF_FALSE(self, addr, target):
        return self.POP_JUMP_IF(addr, target, truthiness=False)

    def POP_JUMP_IF_TRUE(self, addr, target):
        return self.POP_JUMP_IF(addr, target, truthiness=True)

    def JUMP_ABSOLUTE(self, addr, target):
        # print("*** JUMP ABSOLUTE ***", addr)
        # return addr.jump()
        pass
    
    #
    # For loops
    #
    
    def GET_ITER(self, addr):
        pass
    
    def FOR_ITER(self, addr, delta):
        iterable = self.stack.pop()
        jump_addr = addr.jump()
        d_body = SuiteDecompiler(addr[1], jump_addr[-1])
        for_stmt = ForStatement(iterable)
        d_body.stack.push(for_stmt)
        d_body.run()
        for_stmt.body = d_body.suite
        self.suite.add_statement(for_stmt)
        return jump_addr
    
    # Function creation

    def MAKE_FUNCTION(self, addr, argc, is_closure=False):
        code = Code(self.stack.pop().val, self.code)
        closure = self.stack.pop() if is_closure else None
        defaults = self.stack.pop(argc & 0xFF)
        kwdefaults = {}
        for i in range(argc >> 8):
            k, v = self.stack.pop(2)
            kwdefaults[k.name] = v
        func_maker = code_map.get(code.name, DefStatement)
        self.stack.push(func_maker(code, defaults, kwdefaults, closure))
    
    def LOAD_CLOSURE(self, addr, i):
        # Push the varname.  It doesn't matter as it is not used for now.
        self.stack.push(self.code.derefnames[i])

    def MAKE_CLOSURE(self, addr, argc):
        self.MAKE_FUNCTION(addr, argc, is_closure=True)

    #
    # Raising exceptions
    #

    def RAISE_VARARGS(self, addr, argc):
        # TODO: find out when argc is 2 or 3
        # Answer: In Python 3, only 0, 1, or 2 argument (see PEP 3109)
        if argc == 0:
            self.write("raise")
        elif argc == 1:
            exception = self.stack.pop()
            self.write("raise {}", exception)
        elif argc == 2:
            exc, from_exc = self.stack.pop()
            self.write("raise {} from {}". exc, from_exc)
        else:
            raise Unknown


# Create unary operators types and opcode handlers
for op, name, ptn, prec in unary_ops:
    name = 'Py' + name
    tp = type(name, (PyUnaryOp,), dict(pattern=ptn, precedence=prec))
    globals()[name] = tp
    def method(self, addr, tp=tp):
        tp.instr(self.stack)
    setattr(SuiteDecompiler, op, method)

# Create binary operators types and opcode handlers
for op, name, ptn, prec, inplace_ptn in binary_ops:
    # Create the binary operator
    tp_name = 'Py' + name
    tp = globals().get(tp_name, None)
    if tp is None:
        tp = type(tp_name, (PyBinaryOp,), dict(pattern=ptn, precedence=prec))
        globals()[tp_name] = tp
    def method(self, addr, tp=tp):
        tp.instr(self.stack)
    setattr(SuiteDecompiler, 'BINARY_' + op, method)
    # Create the in-place operation
    if inplace_ptn is not None:
        inplace_op = "INPLACE_" + op
        tp_name = 'InPlace' + name
        tp = type(tp_name, (InPlaceOp,), dict(pattern=inplace_ptn))
        globals()[tp_name] = tp
        def method(self, addr, tp=tp):
            left, right = self.stack.pop(2)
            self.stack.push(tp(left, right))
        setattr(SuiteDecompiler, inplace_op, method)


def test_suite():
    def foo(x):
        x = x*(f(x) + 1 - x[1])
        x = (y, [z, t]), {1, 2, 3}
        t = {1:[x, y], 3:'x'}
        a.x.y, b[2] = b, a
        t = 1 <= x < 2 < y
        g(x, y + 1, x=12)
        x = a and (b or c)
        y = 1 if x else 2
        z = 1 if not x else 2
        a = b = 3
        x[y.z] = a, b = u
        if x:
            f(x)
            del x
        else:
            g(x)
            h[y] = 3
        if y:
            foo()
        if x:
            a()
            if z:
                a1()
            else:
                a2()
            b()
        elif y:
            b()
        else:
            c()
        x = a and b or c
        return "hello"
    def foo1():
        if a and ((b and c and d) or e or f) and g: g()
        if a or (b and (c1 or c2) and d) or e: g()
        if a and b or c: g()
        if a or b and c: g()
        if a and (b or c): g()
    def foo1():
        x = a and b or c
        x = a and (b1 or b2) and c or c
        x = (a and b) + (c or (not d and e))
    def foo1():
        def f(x, y=2):
            return x + y if x else x - y
        g = lambda x: x + 1
    def foo1(x):
        x += 2
        x[3] *= 10
    def foo1(x):
        while f(x):
            if x and y:
                g(x)
            else:
                x + 2
            x += f(x, y=2)
        while a and b:
            while c and d:
                print(a, c)
    def foo1(x):
        for i in x:
            print(i)
        for a, b in x:
            for c, (d, e) in a:
                print(a + c)
    def foo1(x):
        for i in x:
            if i == 2:
                f()
            else:
                g()
        for i in x:
            if i:
                break
        while x:
            if x:
                f()
    def foo():
        try:
            x = 1
        except A:
            x = 2
        except B as b:
            x = 3
        try:
            x = 2
            y = 3
        except A:
            x = 5
        finally:
            z = 2
        try:
            frobz()
        except:
            bar()
        finally:
            frobn()
    def foo1(fname):
        with open(fname) as f:
            for line in f:
                print(line)
        with x as y, s as t:
            bar()
    def foo1():
        l = [x for x in y for z in x]
        l1 = [x for x in y if f(x)]
        s = {x + 1 for x, y in T}
        d = {x: y for x, y in f(a)}
    def foo1():
        class A:
            def f(self): return 1
        class B(A, metaclass=MyType):
            bar = 12
            def __init__(self, x):
                self.x = x
    def foo1():
        g = (x for x in y)
        f(y - 2 for x in S for y in f(x))
    def foo1():
        def g(x):
            for i in x:
                yield f(i) + 2
            a = yield 5
            b = 1 + (yield 12)
    def foo(x, y):
        def f(z):
            return z + x
        def g(z):
            global x
            return z + x
        def h(z):
            nonlocal x
            x = 12
    def foo():
        if a:
            return
        if b:
            foo()
            if c:
                return
    foo = SuiteDecompiler.POP_JUMP_IF
    def foo1():
        if a:
            if b:
                f()
            elif c:
                g()
    def foo1():
        if a:
            if b:
                f()
        elif c:
            g()
    def foo():
        assert a, b
    def foo1():
        assert a
    def foo1():
        raise
    def foo():
        @decorate
        def f(): pass
        @foo
        @bar.baz(3)
        class A: pass
    def foo():
        class B(A):
            def foo(): pass
            def bar(): pass
    dis.dis(foo)
    code = Code(foo.__code__)
    code.show()
    dec = SuiteDecompiler(code[0])
    dec.run()
    dec.suite.display()


if __name__ == "__main__":
    print("testing...")
    test_suite()
