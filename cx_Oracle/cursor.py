from ctypes import byref
import ctypes

from custom_exceptions import InterfaceError, ProgrammingError, DatabaseError
from buffer import cxBuffer
import oci
from variable import Variable
from objectvar import OBJECTVAR
from utils import is_sequence

class Cursor(object):
    def __init__(self, connection):
        """Create a new cursor object."""
        self.connection = connection # public interface
        self.environment = connection.environment
        self.arraysize = 50 # public
        self.fetch_array_size = 50
        self.bindarraysize = 1 # public
        self.statement_type = -1
        self.output_size = -1
        self.output_size_column = -1
        self.is_open = True

        self.handle = oci.POINTER(oci.OCIStmt)()
        self.statement = None # public interface
        self.input_sizes = 0
        self.numbersAsStrings = None # public interface
        self.inputtypehandler = None # public interface
        self.outputtypehandler = None # public interface
        self.rowfactory = None # public interface

    def raise_if_not_open(self):
        if not self.is_open:
            raise InterfaceError("not open")
        
        return self.connection.raise_if_not_connected()

    def free_handle(self, raise_exception):
        """Free the handle which may be reallocated if necessary."""
        if self.handle:
            if self.is_owned:
                status = oci.OCIHandleFree(self.handle, oci.OCI_HTYPE_STMT)
                if raise_exception:
                    self.environment.check_for_error(status, "Cursor_FreeHandle()")
            elif self.connection.handle:
                try:
                    buffer = cxBuffer.new_from_object(self.statement_tag, self.environment.encoding)
                except:
                    if raise_exception:
                        raise

                status = oci.OCIStmtRelease(self.handle, self.environment.error_handle, buffer.c_struct.ptr, buffer.c_struct.size, oci.OCI_DEFAULT)

                if raise_exception:
                    self.environment.check_for_error(status, "Cursor_FreeHandle()")

            self.handle = oci.POINTER(oci.OCIStmt)()

    def internal_prepare(self, statement, statement_tag):
        """Internal method for preparing a statement for execution."""

        # make sure we don't get a situation where nothing is to be executed
        if statement is None and not self.statement:
            raise ProgrammingError("no statement specified and no prior statement prepared")

        # nothing to do if the statement is identical to the one already stored
        # but go ahead and prepare anyway for create, alter and drop statments
        if statement is None or statement == self.statement:
            if self.statement_type not in (oci.OCI_STMT_CREATE, oci.OCI_STMT_DROP, oci.OCI_STMT_ALTER):
                return
            statement = self.statement

        # keep track of the statement
        self.statement = statement

        # release existing statement, if necessary
        self.statement_tag = statement_tag
        self.free_handle(True)

        # prepare statement
        self.is_owned = False
        statement_buffer = cxBuffer.new_from_object(statement, self.environment.encoding)

        tag_buffer = cxBuffer.new_from_object(statement_tag, self.environment.encoding)

        status = oci.OCIStmtPrepare2(self.connection.handle, byref(self.handle), self.environment.error_handle, statement_buffer.c_struct.ptr, statement_buffer.c_struct.size, tag_buffer.c_struct.ptr, tag_buffer.c_struct.size, oci.OCI_NTV_SYNTAX, oci.OCI_DEFAULT);

        try:
            self.environment.check_for_error(status, "Cursor_InternalPrepare(): prepare")
        except:
            # this is needed to avoid "invalid handle" errors since Oracle doesn't
            # seem to leave the pointer alone when an error is raised but the
            # resulting handle is still invalid
            self.handle = oci.POINTER(oci.OCIStmt)()
            raise

        # clear bind variables, if applicable
        if not self.input_sizes:
            self.bindvars = None

        # clear row factory, if applicable
        self.row_factory = None

        # determine if statement is a query
        self.get_statement_type() # cx oracle is not checking anything here, or is it?

    def get_statement_type(self):
        c_statement_type = oci.ub2()

        status = oci.OCIAttrGet(self.handle, oci.OCI_HTYPE_STMT, byref(c_statement_type), 0, oci.OCI_ATTR_STMT_TYPE, self.environment.error_handle)
        self.environment.check_for_error(status, "Cursor_GetStatementType()")
        self.statement_type = c_statement_type.value
        self.fetch_variables = None

    def perform_define(self):
        c_num_params = ctypes.c_int()

        # determine number of items in select-list
        status = oci.OCIAttrGet(self.handle, oci.OCI_HTYPE_STMT, byref(c_num_params), 0, oci.OCI_ATTR_PARAM_COUNT, self.environment.error_handle)
        self.environment.check_for_error(status, "Cursor_PerformDefine()")

        num_params = c_num_params.value

        # create a list corresponding to the number of items
        self.fetch_variables = [None] * num_params # or should I use appends?

        # define a variable for each select-item
        self.fetch_array_size = self.arraysize
        for pos in xrange(1, num_params+1):
            var = Variable.define(self, self.fetch_array_size, pos)
            self.fetch_variables[pos - 1] = var

    def internal_execute(self, num_iters):
        """Perform the work of executing a cursor and set the rowcount appropriately
           regardless of whether an error takes place."""

        if self.connection.autocommit:
            mode = oci.OCI_COMMIT_ON_SUCCESS
        else:   
            mode = oci.OCI_DEFAULT

        status = oci.OCIStmtExecute(self.connection.handle, self.handle, self.environment.error_handle, num_iters, 0, 0, 0, mode)

        try:
            self.environment.check_for_error(status, "Cursor_InternalExecute()")
        except Exception, e:
            new_exception = self.set_error_offset(e)
            try:
                self.set_row_count()
            except:
                pass
            raise new_exception

        return self.set_row_count()

    def set_bind_variables(self, parameters, num_elements, array_pos, defer_type_assignment):
        """Create or set bind variables."""
        # make sure positional and named binds are not being intermixed
        num_params = 0
        bound_by_pos = is_sequence(parameters)
        if bound_by_pos:
            num_params = len(parameters)

        if self.bindvars:
            orig_bound_by_pos = isinstance(self.bindvars, list)
            if bound_by_pos != orig_bound_by_pos:
                raise ProgrammingError("positional and named binds cannot be intermixed")

            orig_num_params = len(self.bindvars)

        # otherwise, create the list or dictionary if needed
        else:
            if bound_by_pos:
                self.bindvars = [None] * num_params
            else:
                self.bindvars = {}

            orig_num_params = 0
        
        # handle positional binds
        if bound_by_pos:
            for i, value in enumerate(parameters):
                if i < orig_num_params:
                    orig_var = self.bindvars[i]
                else:
                    orig_var = None
                
                new_var = self.set_bind_variable_helper(num_elements, array_pos, value, orig_var, defer_type_assignment)

                if new_var:
                    if i < len(self.bindvars):
                        self.bindvars[i] = new_var
                    else:
                        self.bindvars.append(new_var)

        # handle named binds
        else:
            for key, value in parameters.iteritems():
                orig_var = self.bindvars.get(key, None)
                new_var = self.set_bind_variable_helper(num_elements, array_pos, value, orig_var, defer_type_assignment)

                if new_var:
                    self.bindvars[key] = new_var

    def set_bind_variable_helper(self, num_elements, array_pos, value, orig_var, defer_type_assignment):
        """Helper for setting a bind variable."""

        # initialization
        new_var = None 
        is_value_var = isinstance(value, Variable)

        # handle case where variable is already bound
        if orig_var:
            # if the value is a variable object, rebind it if necessary
            if is_value_var:
                if orig_var != value:
                    new_var = value

            # if the number of elements has changed, create a new variable
            # this is only necessary for executemany() since execute() always
            # passes a value of 1 for the number of elements
            elif num_elements > orig_var.numElements:
                new_var = Variable(self, num_elements, orig_var.type, orig_var.size)
                new_var.set_value(array_pos, value)

            # otherwise, attempt to set the value
            else:
                try:
                    orig_var.set_value(array_pos, value)
                except:
                    # executemany() should simply fail after the first element
                    if array_pos > 0:
                        raise
                    
                    # anything other than index error or type error should fail
                    if isinstance(e, (IndexError, TypeError)):
                        raise

                    orig_var = None

        # if no original variable used, create a new one
        if not orig_var:

            # if the value is a variable object, bind it directly
            if is_value_var:
                new_var = value
                new_var.bound_pos = 0
                new_var.bound_name = None

            # otherwise, create a new variable, unless the value is None and
            # we wish to defer type assignment
            elif value is not None or not defer_type_assignment:
                new_var = Variable.new_by_value(self, value, num_elements)
                new_var.set_value(array_pos, value)

        return new_var


    def execute(self, statement, *args, **kwargs):
        """Execute the statement."""
        execute_args = None

        if args:
            execute_args = args[0]

        if execute_args and kwargs:
            raise InterfaceError("expecting argument or keyword arguments, not both")
        
        if kwargs:
            execute_args = kwargs

        if execute_args:
            if not (isinstance(execute_args, dict) or execute_args.__getitem__):
                raise TypeError("expecting a dictionary, sequence or keyword args")

        # make sure the cursor is open
        self.raise_if_not_open()

        # prepare the statement, if applicable
        self.internal_prepare(statement, None)
        
        # perform binds
        if execute_args:
            self.set_bind_variables(execute_args, 1, 0, 0)

        self.perform_bind()
        
        # execute the statement
        is_query = self.statement_type == oci.OCI_STMT_SELECT
        if is_query:
            num_iters = 0
        else:
            num_iters = 1

        self.internal_execute(num_iters)
        
        # perform defines, if necessary
        if is_query and not self.fetch_variables:
            self.perform_define()

        # reset the values of setoutputsize()
        self.output_size = -1
        self.output_size_column = -1

        # for queries, return the cursor for convenience
        if is_query:
            return self

        # for all other statements, simply return None
        return None

    def prepare(self, statement, statement_tag=None):
        """Prepare the statement for execution."""

        # make sure the cursor is open
        self.raise_if_not_open()

        # prepare the statement
        self.internal_prepare(statement, statement_tag)

    def executemany(self, statement, list_of_arguments):
        """Execute the statement many times. The number of times is equivalent to the number of elements in the array 
of dictionaries."""
        # expect statement text (optional) plus list of mappings
        #if (!PyArg_ParseTuple(args, "OO!", &statement, &PyList_Type,
        #        &listOfArguments))
        #    return NULL

        # make sure the cursor is open - ctypes: prepare already checks that
        # self.raise_if_not_open()

        # prepare the statement
        self.prepare(statement, None)

        # queries are not supported as the result is undefined
        if self.statement_type == oci.OCI_STMT_SELECT:
            raise NotSupportedError("queries not supported: results undefined")

        # perform binds
        num_rows = len(list_of_arguments)
        for i, arguments in enumerate(list_of_arguments):
            if not isinstance(arguments, dict) and not is_sequence(arguments):
                raise InterfaceError("expecting a list of dictionaries or sequences")
            self.set_bind_variables(arguments, num_rows, i, (i < num_rows - 1))

        self.perform_bind()

        # execute the statement, but only if the number of rows is greater than zero since Oracle raises an error 
        # otherwise
        if num_rows > 0:
            self.internal_execute(num_rows)

    def set_row_count(self):
        """Set the rowcount variable."""
        # rowcount is not row_count because it is public interface

        if self.statement_type == oci.OCI_STMT_SELECT:
            self.rowcount = 0
            self.actual_rows = -1 # not public interface
            self.row_num = 0
        else:
            if self.statement_type in (oci.OCI_STMT_INSERT, oci.OCI_STMT_UPDATE, oci.OCI_STMT_DELETE):
                c_row_count = oci.ub4()
                status = oci.OCIAttrGet(self.handle, oci.OCI_HTYPE_STMT, byref(c_row_count), 0, oci.OCI_ATTR_ROW_COUNT, self.environment.error_handle)
                self.environment.check_for_error(status, "Cursor_SetRowCount()")
                self.rowcount = c_row_count.value
            else:
                self.rowcount = -1

    def perform_bind(self):
        """Perform the binds on the cursor."""

        # ensure that input sizes are reset
        # this is done before binding is attempted so that if binding fails and
        # a new statement is prepared, the bind variables will be reset and
        # spurious errors will not occur
        self.input_sizes = 0

        # set values and perform binds for all bind variables
        if self.bindvars:
            if isinstance(self.bindvars, dict):
                for key, var in self.bindvars.iteritems():
                    var.bind(self, key, 0)
            else:
                for i, var in enumerate(self.bindvars):
                    if var is not None:
                        var.bind(self, None, i + 1)

    def fixup_bound_cursor(self):
        """Fixup a cursor so that fetching and returning cursor descriptions are successful after binding a cursor to another cursor."""
        if self.handle and self.statement_type < 0:
            self.get_statement_type()
            try:
                self.perform_define()
            except:
                if self.statement_type == oci.OCI_STMT_SELECT:
                    raise

            self.set_row_count()
    
    def verify_fetch(self):
        self.raise_if_not_open()
        self.fixup_bound_cursor()

        if self.statement_type != oci.OCI_STMT_SELECT:
            raise InterfaceError("not a query")

    def internal_fetch(self, num_rows):
        """Performs the actual fetch from Oracle."""
        
        if not self.fetch_variables:
            raise InterfaceError("query not executed")

        for var in self.fetch_variables:
            var.internal_fetch_num += 1
            if var.type.pre_fetch_proc:
                var.type.pre_fetch_proc(var)
            
        status = oci.OCIStmtFetch(self.handle, self.environment.error_handle, num_rows, oci.OCI_FETCH_NEXT, oci.OCI_DEFAULT)

        if status != oci.OCI_NO_DATA:
            self.environment.check_for_error(status, "Cursor_InternalFetch(): fetch")

        row_count = oci.ub4()
        status = oci.OCIAttrGet(self.handle, oci.OCI_HTYPE_STMT, byref(row_count), 0, oci.OCI_ATTR_ROW_COUNT, self.environment.error_handle)
        self.environment.check_for_error(status, "Cursor_InternalFetch(): row count")

        self.actual_rows = row_count.value - self.rowcount
        self.row_num = 0

    def create_row(self):
        """Create an object for the row. The object created is a tuple unless a row
           factory function has been defined in which case it is the result of the
           row factory function called with the argument tuple that would otherwise be
           returned."""

        # create a new tuple
        num_items = len(self.fetch_variables)
        tuple = [None] * num_items

        # acquire the value for each item
        for pos in xrange(num_items):
            var = self.fetch_variables[pos]
            item = var.getvalue(self.row_num)
            tuple[pos] = item

        # increment row counters
        self.row_num += 1
        self.rowcount += 1

        # if a row factory is defined, call it
        if self.rowfactory is not None:
            return self.rowfactory(tuple)

        return tuple

    def more_rows(self):
        """Returns a boolean indicating if more rows can be retrieved from the cursor."""
        if self.row_num >= self.actual_rows:
            if self.actual_rows < 0 or self.actual_rows == self.fetch_array_size:
                self.internal_fetch(self.fetch_array_size)

            if self.row_num >= self.actual_rows:
                return False

        return True
    
    def fetchone(self): # public
        """Fetch a single row from the cursor."""

        # verify fetch can be performed
        self.verify_fetch()

        # setup return value
        more_rows_to_fetch = self.more_rows()
        if not more_rows_to_fetch:
            return None
        return self.create_row()

    def fetchall(self):
        """Fetch all remaining rows from the cursor."""
        self.verify_fetch()
        return self.multi_fetch(0)

    def multi_fetch(self, row_limit):
        """Return a list consisting of the remaining rows up to the given row limit (if specified)."""

        results = []

        # fetch as many rows as possible
        row_num = 0
        while row_limit == 0 or row_num < row_limit:
            more_rows_available = self.more_rows()
            if more_rows_available:
                row = self.create_row()
                results.append(row)
            else:
                break
            row_num += 1

        return results
    
    def set_error_offset(self, exception):
        """Set the error offset on the error object, if applicable."""
        if isinstance(exception, DatabaseError):
            error = exception.args[0]
            c_offset = oci.ub4()
            oci.OCIAttrGet(self.handle, oci.OCI_HTYPE_STMT, byref(c_offset), 0, oci.OCI_ATTR_PARSE_ERROR_OFFSET, self.environment.error_handle)
            error.offset = c_offset.value

        return exception



    def call_build_statement(self, name, return_value, list_of_arguments, keyword_arguments):
        """Determine the statement and the bind variables to bind to the statement that is created for calling a stored procedure or function."""

        # initialize the bind variables to the list of positional arguments
        if list_of_arguments:
            bind_variables = list(list_of_arguments) # copy to avoid messing up with the sequence from the user?
        else:
            bind_variables = []

        # insert the return variable, if applicable
        if return_value:
            bind_variables.insert(0, return_value)

        # initialize format arguments
        format_args = [name]

        # begin building the statement_template
        arg_num = 1
        statement_template = 'begin '

        if return_value:
            statement_template += ":1 := "
            arg_num += 1

        statement_template += "%s ("

        # include any positional arguments first
        if list_of_arguments:
            for i, argument in enumerate(list_of_arguments):
                if i > 0:
                    statement_template += ','
                statement_template += ":%d" % arg_num
                arg_num += 1
                if isinstance(argument, bool):
                    statement_template += " = 1"

        # next append any keyword arguments
        if keyword_arguments:
            pos = 0
            for key, value in keyword_arguments.iteritems():
                bind_variables.append(value)
                format_args.append(key)
                if (arg_num > 1 and not return_value) or (arg_num > 2 and return_value):
                    statement_template += ','
                statement_template += "%%s => :%d" % arg_num
                arg_num += 1
                if isinstance(value, bool):
                    statement_template +=  " = 1"

        statement_template += "); end;"

        
        statement = statement_template % tuple(format_args)

        return statement, bind_variables

    def call(self, return_value, name, list_of_arguments, keyword_arguments): # kwargs are the stored procedure kwargs, not Python!
        """Call a stored procedure or function."""

        # verify that the arguments are passed correctly
        if list_of_arguments:
            if not hasattr(list_of_arguments, '__getitem__'):
                raise TypeError("arguments must be a sequence")

        # make sure the cursor is open
        self.raise_if_not_open()

        # determine the statement to execute and the argument to pass
        statement, bind_variables = self.call_build_statement(name, return_value, list_of_arguments, keyword_arguments)

        # execute the statement on the cursor
        self.execute(statement, bind_variables)

    def callproc(self, name, parameters=None, keywordParameters=None):
        """Call a stored procedure and return the (possibly modified) arguments."""
        # call the stored procedure
        self.call(None, name, parameters, keywordParameters)

        # create the return value
        results = [var.getvalue(0) for var in self.bindvars]
        return results

    def callfunc(self, name, return_type, parameters=None, keywordParameters=None):
        """Call a stored function and return the return value of the function."""

        # create the return variable
        var = Variable.new_by_type(self, return_type, 1)

        # call the function
        self.call(var, name, parameters, keywordParameters)

        # determine the results
        results = var.getvalue(0)
        return results

    def close(self):
        # make sure we are actually open
        self.raise_if_not_open()

        # close the cursor
        self.free_handle(True)

        self.is_open = False
        
    #def get_bind_names(self, num_elements):
        #"""Return a list of bind variable names. At this point the cursor must have already been prepared."""
        ##udt_Cursor *self,                   // cursor to get information from
        ##int numElements,                    // number of elements (IN/OUT)
        ##PyObject **names)                   // list of names (OUT)
        ##ub1 *bindNameLengths, *indicatorNameLengths, *duplicate;
        ##char *buffer, **bindNames, **indicatorNames;
        ##OCIBind **bindHandles;
        ##int elementSize, i;
        ##sb4 foundElements;
        ##PyObject *temp;
        ##sword status;
    
        ## ensure that a statement has already been prepared
        #if not self.statement:
            #raise ProgrammingError("statement must be prepared first")
        
        ## avoid bus errors on 64-bit platforms
        #num_elements = num_elements + (ctypes.sizeof(ctypes.c_void_p) - num_elements % ctypes.sizeof(ctypes.c_void_p))
    
        ## initialize the buffers
        #element_size = ctypes.sizeof(ctypes.c_char_p) + ctypes.sizeof(oci.ub1) + ctypes.sizeof(ctypes.c_char_p) + ctypes.sizeof(oci.ub1) + \
                #ctypes.sizeof(oci.ub1) + ctypes.sizeof(oci.POINTER(oci.OCIBind))
        #buffer = ctypes.create_string_buffer(num_elements * element_size)
        #bindNames = (char**) buffer;
        #bindNameLengths = (ub1*) (((char*) bindNames) +
                #sizeof(char*) * numElements);
        #indicatorNames = (char**) (((char*) bindNameLengths) +
                #sizeof(ub1) * numElements);
        #indicatorNameLengths = (ub1*) (((char*) indicatorNames) +
                #sizeof(char*) * numElements);
        #duplicate = (ub1*) (((char*) indicatorNameLengths) +
                #sizeof(ub1) * numElements);
        #bindHandles = (OCIBind**) (((char*) duplicate) +
                #sizeof(ub1) * numElements);
    
        ## get the bind information
        #status = OCIStmtGetBindInfo(self->handle,
                #self->environment->errorHandle, numElements, 1, &foundElements,
                #(text**) bindNames, bindNameLengths, (text**) indicatorNames,
                #indicatorNameLengths, duplicate, bindHandles);
        #if (status != OCI_NO_DATA &&
                #Environment_CheckForError(self->environment, status,
                #"Cursor_GetBindNames()") < 0) {
            #PyMem_Free(buffer);
            #return -1;
        #}
        #if (foundElements < 0) {
            #*names = NULL;
            #PyMem_Free(buffer);
            #return abs(foundElements);
        #}
    
        ## create the list which is to be returned
        #*names = PyList_New(0);
        #if (!*names) {
            #PyMem_Free(buffer);
            #return -1;
        #}
    
        ## process the bind information returned
        #for (i = 0; i < foundElements; i++) {
            #if (!duplicate[i]) {
                #temp = cxString_FromEncodedString(bindNames[i],
                        #bindNameLengths[i],
                        #self->connection->environment->encoding);
                #if (!temp) {
                    #Py_DECREF(*names);
                    #PyMem_Free(buffer);
                    #return -1;
                #}
                #if (PyList_Append(*names, temp) < 0) {
                    #Py_DECREF(*names);
                    #Py_DECREF(temp);
                    #PyMem_Free(buffer);
                    #return -1;
                #}
                #Py_DECREF(temp);
            #}
        #}
        #PyMem_Free(buffer);
    
        #return 0;
    #}

    def bindnames(self):
        """Return a list of bind variable names."""

        # make sure the cursor is open
        self.raise_if_not_open()

        # return result
        result, names = self.get_bind_names(8)
        if result < 0:
            return None
        result, names = self.get_bind_names(result)
        if not names and result < 0:
            return None
        return names

    def __iter__(self):
        """Return a reference to the cursor which supports the iterator protocol."""
        self.verify_fetch()
        return self
    
    def next(self):
        """Return a reference to the cursor which supports the iterator protocol."""
        self.verify_fetch()
        more_rows_available = self.more_rows()
        if more_rows_available:
            return self.create_row()
        
        raise StopIteration() # TODO: is this right?

    def __del__(self):
        self.free_handle(False)
        
    def var(self, type, size=0, arraysize=None, inconverter=None, outconverter=None, typename=None):
        """Create a bind variable and return it."""

        # parse arguments
        if arraysize is None:
            arraysize = self.bindarraysize
            
        array_size = arraysize # ctypes: normalize name
        
        # determine the type of variable
        var_type = Variable.type_by_python_type(self, type)
        if var_type.is_variable_length and size == 0:
            size = var_type.size
        
        if type is OBJECTVAR and not typename:
            raise TypeError("expecting type name for object variables")
    
        # create the variable
        var = Variable(self, array_size, var_type, size)
        var.inconverter = inconverter
        var.outconverter = outconverter
    
        # define the object type if needed
        if type is OBJECTVAR:
            var.object_type = ObjectType.new_by_name(self.connection, typeName)
        
        return var
    
    def setinputsizes(self, *args, **kwargs):
        """Set the sizes of the bind variables."""
        # only expect keyword arguments or positional arguments, not both
        if args and kwargs:
            raise InterfaceError("expecting arguments or keyword arguments, not both")
        
        self.raise_if_not_open()
    
        # eliminate existing bind variables
        if kwargs:
            self.bindvars = {}
        else:
            self.bindvars = [None] * len(args)
        
        self.input_sizes = 1
    
        # process each input
        if kwargs:
            for key, value in kwargs.iteritems():
                var = Variable.new_by_type(self, value, self.bindarraysize)
                self.bindvars[key] = var
        else:
            for i, value in enumerate(args):
                if value is None:
                    var = None
                else:
                    var = Variable.new_by_type(self, value, self.bindarraysize)
                
                self.bindvars[i] = var
        
        return self.bindvars