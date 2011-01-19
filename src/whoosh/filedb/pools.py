
#===============================================================================
# Copyright 2010 Matt Chaput
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#    http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#===============================================================================

import os, tempfile
from array import array
from collections import defaultdict
from heapq import heapify, heappush, heappop
from marshal import load, dump
import sqlite3 as sqlite

from whoosh.filedb.filetables import LengthWriter, LengthReader
from whoosh.util import length_to_byte, now


def imerge(iterators):
    """Merge-sorts items from a list of iterators.
    """
    
    # The list of "current" head items from the iterators
    current = []
    
    # Initialize the current list with the first item from each iterator
    for g in iterators:
        try:
            current.append((g.next(), g))
        except StopIteration:
            pass
        
    # Turn the current list into a heap structure
    heapify(current)
    
    # While there are multiple iterators in the current list, pop the lowest
    # item and refill from the popped item's iterator
    while len(current) > 1:
        item, gen = heappop(current)
        yield item
        try:
            heappush(current, (gen.next(), gen))
        except StopIteration:
            pass
    
    # If there's only one iterator left, shortcut to simply yield all items
    # from the iterator. This is faster than popping and refilling the heap.
    if current:
        item, gen = current[0]
        yield item
        for item in gen:
            yield item


def read_run(filename, count):
    f = open(filename, "rb")
    while count:
        count -= 1
        yield load(f)
    f.close()


def write_postings(schema, termtable, lengths, postwriter, postiter,
                   inlinelimit=1):
    # This method pulls postings out of the posting pool (built up as
    # documents are added) and writes them to the posting file. Each time
    # it encounters a posting for a new term, it writes the previous term
    # to the term index (by waiting to write the term entry, we can easily
    # count the document frequency and sum the terms by looking at the
    # postings).

    current_fieldname = None # Field number of the current term
    current_text = None # Text of the current term
    first = True
    current_weight = 0
    offset = None
    getlength = lengths.get
    format = None

    # Loop through the postings in the pool. Postings always come out of the
    # pool in (field number, lexical) order.
    for fieldname, text, docnum, weight, valuestring in postiter:
        # Is this the first time through, or is this a new term?
        if first or fieldname > current_fieldname or text > current_text:
            if first:
                first = False
            else:
                # This is a new term, so finish the postings and add the
                # term to the term table
                
                postcount = postwriter.posttotal
                # If the number of posts is below a certain threshold,
                # inline them in the "offset" argument.
                if postcount <= inlinelimit and postwriter.blockcount < 1:
                    offset = postwriter.as_inline()
                    postwriter.cancel()
                else:
                    postwriter.finish()
                
                termtable.add((current_fieldname, current_text),
                              (current_weight, offset, postcount))

            # Reset the post writer and the term variables
            if fieldname != current_fieldname:
                format = schema[fieldname].format
                current_fieldname = fieldname
            current_text = text
            current_weight = 0
            offset = postwriter.start(format)

        elif (fieldname < current_fieldname
              or (fieldname == current_fieldname and text < current_text)):
            # This should never happen!
            raise Exception("Postings are out of order: %r:%s .. %r:%s" %
                            (current_fieldname, current_text, fieldname, text))

        # Write a posting for this occurrence of the current term
        current_weight += weight
        postwriter.write(docnum, weight, valuestring, getlength(docnum, fieldname))

    # If there are still "uncommitted" postings at the end, finish them off
    if not first:
        postcount = postwriter.finish()
        termtable.add((current_fieldname, current_text),
                      (current_weight, offset, postcount))


class PoolBase(object):
    def __init__(self, schema, dir=None, basename=''):
        self.schema = schema
        self._using_tempdir = False
        self.dir = dir
        self._using_tempdir = dir is not None
        self.basename = basename
        
        self.length_arrays = {}
        self._fieldlength_totals = defaultdict(int)
        self._fieldlength_maxes = {}
    
    def _make_dir(self):
        if self.dir is None:
            self.dir = tempfile.mkdtemp(".whoosh")
    
    def _filename(self, name):
        return os.path.abspath(os.path.join(self.dir, self.basename + name))
    
    def _clean_temp_dir(self):
        if self._using_tempdir and self.dir and os.path.exists(self.dir):
            try:
                os.rmdir(self.dir)
            except OSError:
                # directory didn't exist or was not empty -- don't
                # accidentially delete data
                pass
    
    def cleanup(self):
        self._clean_temp_dir()
    
    def cancel(self):
        pass
    
    def fieldlength_totals(self):
        return dict(self._fieldlength_totals)
    
    def fieldlength_maxes(self):
        return self._fieldlength_maxes
    
    def add_posting(self, fieldname, text, docnum, weight, valuestring):
        raise NotImplementedError
    
    def add_field_length(self, docnum, fieldname, length):
        self._fieldlength_totals[fieldname] += length
        if length > self._fieldlength_maxes.get(fieldname, 0):
            self._fieldlength_maxes[fieldname] = length
        
        if fieldname not in self.length_arrays:
            self.length_arrays[fieldname] = array("B")
        arry = self.length_arrays[fieldname]
        
        if len(arry) <= docnum:
            for _ in xrange(docnum - len(arry) + 1):
                arry.append(0)
        arry[docnum] = length_to_byte(length)
    
    def _fill_lengths(self, doccount):
        for fieldname in self.length_arrays.keys():
            arry = self.length_arrays[fieldname]
            if len(arry) < doccount:
                for _ in xrange(doccount - len(arry)):
                    arry.append(0)
    
    def add_content(self, docnum, fieldname, field, value):
        add_posting = self.add_posting
        termcount = 0
        # TODO: Method for adding progressive field values, ie
        # setting start_pos/start_char?
        for w, freq, weight, valuestring in field.index(value):
            #assert w != ""
            add_posting(fieldname, w, docnum, weight, valuestring)
            termcount += freq
        
        if field.scorable and termcount:
            self.add_field_length(docnum, fieldname, termcount)
            
        return termcount
    
    def _write_lengths(self, lengthfile, doccount):
        self._fill_lengths(doccount)
        lw = LengthWriter(lengthfile, doccount, lengths=self.length_arrays)
        lw.close()


class TempfilePool(PoolBase):
    def __init__(self, schema, limitmb=32, dir=None, basename='', **kw):
        super(TempfilePool, self).__init__(schema, dir=dir, basename=basename)
        
        self.limit = limitmb * 1024 * 1024
        
        self.size = 0
        self.count = 0
        self.postings = []
        self.runs = []
        
    def add_posting(self, fieldname, text, docnum, weight, valuestring):
        if self.size >= self.limit:
            self.dump_run()

        self.size += len(fieldname) + len(text) + 18
        if valuestring: self.size += len(valuestring)
        
        self.postings.append((fieldname, text, docnum, weight, valuestring))
        self.count += 1
    
    def dump_run(self):
        if self.size > 0:
            #print "Dumping run..."
            t = now()
            self._make_dir()
            fd, filename = tempfile.mkstemp(".run", dir=self.dir)
            runfile = os.fdopen(fd, "w+b")
            self.postings.sort()
            for p in self.postings:
                dump(p, runfile)
            runfile.close()
            
            self.runs.append((filename, self.count))
            self.postings = []
            self.size = 0
            self.count = 0
            #print "Dumping run took", now() - t, "seconds"
    
    def run_filenames(self):
        return [filename for filename, _ in self.runs]
    
    def cancel(self):
        self.cleanup()
    
    def cleanup(self):
        for filename in self.run_filenames():
            if os.path.exists(filename):
                try:
                    os.remove(filename)
                except IOError:
                    pass
                
        self._clean_temp_dir()
        
    def finish(self, doccount, lengthfile, termtable, postingwriter):
        self._write_lengths(lengthfile, doccount)
        lengths = LengthReader(None, doccount, self.length_arrays)
        
        if self.postings or self.runs:
            if self.postings and len(self.runs) == 0:
                self.postings.sort()
                postiter = iter(self.postings)
            elif not self.postings and not self.runs:
                postiter = iter([])
            else:
                self.dump_run()
                postiter = imerge([read_run(runname, count)
                                   for runname, count in self.runs])
        
            write_postings(self.schema, termtable, lengths, postingwriter, postiter)
        self.cleanup()
        

class SqlitePool(PoolBase):
    def __init__(self, schema, dir=None, basename='', limitmb=32, **kwargs):
        super(SqlitePool, self).__init__(schema, dir=dir, basename=basename)
        self._make_dir()
        self.postbuf = defaultdict(list)
        self.bufsize = 0
        self.limit = limitmb * 1024 * 1024
        self.fieldnames = set()
        self._flushed = False
    
    def _field_filename(self, name):
        return self._filename("%s.sqlite" % name)
    
    def _con(self, name):
        filename = self._field_filename(name)
        con = sqlite.connect(filename)
        if name not in self.fieldnames:
            self.fieldnames.add(name)
            con.execute("create table postings (token text, docnum int, weight float, value blob)")
            #con.execute("create index postix on postings (token, docnum)")
        return con
    
    def flush(self):
        for fieldname, lst in self.postbuf.iteritems():
            con = self._con(fieldname)
            con.executemany("insert into postings values (?, ?, ?, ?)", lst)
            con.commit()
            con.close()
        self.postbuf = defaultdict(list)
        self.bufsize = 0
        self._flushed = True
        print "flushed"
    
    def add_posting(self, fieldname, text, docnum, weight, valuestring):
        self.postbuf[fieldname].append((text, docnum, weight, valuestring))
        self.bufsize += len(text) + 8 + len(valuestring)
        if self.bufsize > self.limit:
            self.flush()
    
    def readback(self):
        for name in sorted(self.fieldnames):
            con = self._con(name)
            con.execute("create index postix on postings (token, docnum)")
            for text, docnum, weight, valuestring in con.execute("select * from postings order by token, docnum"):
                yield (name, text, docnum, weight, valuestring)
            con.close()
            os.remove(self._field_filename(name))
        
        if self._using_tempdir and self.dir:
            try:
                os.rmdir(self.dir)
            except OSError:
                # directory didn't exist or was not empty -- don't
                # accidentially delete data
                pass
    
    def readback_buffer(self):
        for fieldname in sorted(self.postbuf.keys()):
            lst = self.postbuf[fieldname]
            lst.sort()
            for text, docnum, weight, valuestring in lst:
                yield (fieldname, text, docnum, weight, valuestring)
            del self.postbuf[fieldname]
            
    def finish(self, doccount, lengthfile, termtable, postingwriter):
        self._write_lengths(lengthfile, doccount)
        lengths = LengthReader(None, doccount, self.length_arrays)
        
        if not self._flushed:
            gen = self.readback_buffer()
        else:
            if self.postbuf:
                self.flush()
            gen = self.readback()
        
        write_postings(self.schema, termtable, lengths, postingwriter, gen)
    

class NullPool(PoolBase):
    def __init__(self, *args, **kwargs):
        self._fieldlength_totals = {}
        self._fieldlength_maxes = {}
    
    def add_content(self, *args):
        pass
    
    def add_posting(self, *args):
        pass
    
    def add_field_length(self, *args, **kwargs):
        pass
    
    def finish(self, *args):
        pass
        

class MemPool(PoolBase):
    def __init__(self, schema, **kwargs):
        super(MemPool, self).__init__(schema)
        self.schema = schema
        self.postbuf = []
        
    def add_posting(self, *item):
        self.postbuf.append(item)
        
    def finish(self, doccount, lengthfile, termtable, postingwriter):
        self._write_lengths(lengthfile, doccount)
        lengths = LengthReader(None, doccount, self.length_arrays)
        self.postbuf.sort()
        write_postings(self.schema, termtable, lengths, postingwriter, self.postbuf)































