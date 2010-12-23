# attributes.py - manages object attributes
# Copyright (C) 2005, 2006, 2007, 2008, 2009, 2010 Michael Bayer mike_mp@zzzcomputing.com
#
# This module is part of SQLAlchemy and is released under
# the MIT License: http://www.opensource.org/licenses/mit-license.php
"""Defines instrumentation for class attributes and their interaction
with instances.

This module is usually not directly visible to user applications, but
defines a large part of the ORM's interactivity.


"""

import operator
from operator import itemgetter

from sqlalchemy import util, event
from sqlalchemy.orm import interfaces, collections, events
import sqlalchemy.exceptions as sa_exc

mapperutil = util.importlater("sqlalchemy.orm", "util")

PASSIVE_NO_RESULT = util.symbol('PASSIVE_NO_RESULT')
ATTR_WAS_SET = util.symbol('ATTR_WAS_SET')
ATTR_EMPTY = util.symbol('ATTR_EMPTY')
NO_VALUE = util.symbol('NO_VALUE')
NEVER_SET = util.symbol('NEVER_SET')

# "passive" get settings
# TODO: the True/False values need to be factored out
PASSIVE_NO_INITIALIZE = True #util.symbol('PASSIVE_NO_INITIALIZE')
"""Symbol indicating that loader callables should
   not be fired off, and a non-initialized attribute 
   should remain that way."""

# this is used by backrefs.
PASSIVE_NO_FETCH = util.symbol('PASSIVE_NO_FETCH')
"""Symbol indicating that loader callables should not be fired off.
   Non-initialized attributes should be initialized to an empty value."""

PASSIVE_ONLY_PERSISTENT = util.symbol('PASSIVE_ONLY_PERSISTENT')
"""Symbol indicating that loader callables should only fire off for
persistent objects.

Loads of "previous" values during change events use this flag.
"""

PASSIVE_OFF = False #util.symbol('PASSIVE_OFF')
"""Symbol indicating that loader callables should be executed."""


class QueryableAttribute(interfaces.PropComparator):
    """Base class for class-bound attributes. """
    
    def __init__(self, class_, key, impl=None, 
                        comparator=None, parententity=None):
        self.class_ = class_
        self.key = key
        self.impl = impl
        self.comparator = comparator
        self.parententity = parententity

        manager = manager_of_class(class_)
        # manager is None in the case of AliasedClass
        if manager:
            # propagate existing event listeners from 
            # immediate superclass
            for base in manager._bases:
                if key in base:
                    self.dispatch.update(base[key].dispatch)

    dispatch = event.dispatcher(events.AttributeEvents)
    dispatch.dispatch_cls.active_history = False
    
    @util.memoized_property
    def _supports_population(self):
        return self.impl.supports_population
        
    def get_history(self, instance, **kwargs):
        return self.impl.get_history(instance_state(instance),
                                        instance_dict(instance), **kwargs)
    
    def __selectable__(self):
        # TODO: conditionally attach this method based on clause_element ?
        return self

    def __clause_element__(self):
        return self.comparator.__clause_element__()

    def label(self, name):
        return self.__clause_element__().label(name)

    def operate(self, op, *other, **kwargs):
        return op(self.comparator, *other, **kwargs)

    def reverse_operate(self, op, other, **kwargs):
        return op(other, self.comparator, **kwargs)

    def hasparent(self, state, optimistic=False):
        return self.impl.hasparent(state, optimistic=optimistic)
    
    def __getattr__(self, key):
        try:
            return getattr(self.comparator, key)
        except AttributeError:
            raise AttributeError(
                    'Neither %r object nor %r object has an attribute %r' % (
                    type(self).__name__, 
                    type(self.comparator).__name__, 
                    key)
            )
        
    def __str__(self):
        return repr(self.parententity) + "." + self.property.key

    @util.memoized_property
    def property(self):
        return self.comparator.property


class InstrumentedAttribute(QueryableAttribute):
    """Class bound instrumented attribute which adds descriptor methods."""

    def __set__(self, instance, value):
        self.impl.set(instance_state(instance), 
                        instance_dict(instance), value, None)

    def __delete__(self, instance):
        self.impl.delete(instance_state(instance), instance_dict(instance))

    def __get__(self, instance, owner):
        if instance is None:
            return self

        dict_ = instance_dict(instance)
        if self._supports_population and self.key in dict_:
            return dict_[self.key]
        else:
            return self.impl.get(instance_state(instance),dict_)

def create_proxied_attribute(descriptor):
    """Create an QueryableAttribute / user descriptor hybrid.

    Returns a new QueryableAttribute type that delegates descriptor
    behavior and getattr() to the given descriptor.
    """

    class Proxy(QueryableAttribute):
        """A combination of InsturmentedAttribute and a regular descriptor."""

        def __init__(self, key, descriptor, comparator, adapter=None):
            self.key = key
            self.descriptor = descriptor
            self._comparator = comparator
            self.adapter = adapter
            
        @util.memoized_property
        def comparator(self):
            if util.callable(self._comparator):
                self._comparator = self._comparator()
            if self.adapter:
                self._comparator = self._comparator.adapted(self.adapter)
            return self._comparator
        
        def adapted(self, adapter):
            return self.__class__(self.key, self.descriptor,
                                       self._comparator,
                                       adapter)

        def __str__(self):
            return self.key
        
        def __getattr__(self, attribute):
            """Delegate __getattr__ to the original descriptor and/or
            comparator."""
            
            try:
                return getattr(descriptor, attribute)
            except AttributeError:
                try:
                    return getattr(self.comparator, attribute)
                except AttributeError:
                    raise AttributeError(
                    'Neither %r object nor %r object has an attribute %r' % (
                    type(descriptor).__name__, 
                    type(self.comparator).__name__, 
                    attribute)
                    )

    Proxy.__name__ = type(descriptor).__name__ + 'Proxy'

    util.monkeypatch_proxied_specials(Proxy, type(descriptor),
                                      name='descriptor',
                                      from_instance=descriptor)
    return Proxy

class AttributeImpl(object):
    """internal implementation for instrumented attributes."""

    def __init__(self, class_, key,
                    callable_, dispatch, trackparent=False, extension=None,
                    compare_function=None, active_history=False, 
                    parent_token=None, expire_missing=True,
                    **kwargs):
        """Construct an AttributeImpl.

        \class_
          associated class
          
        key
          string name of the attribute

        \callable_
          optional function which generates a callable based on a parent
          instance, which produces the "default" values for a scalar or
          collection attribute when it's first accessed, if not present
          already.

        trackparent
          if True, attempt to track if an instance has a parent attached
          to it via this attribute.

        extension
          a single or list of AttributeExtension object(s) which will
          receive set/delete/append/remove/etc. events.  Deprecated.
          The event package is now used.

        compare_function
          a function that compares two values which are normally
          assignable to this attribute.

        active_history
          indicates that get_history() should always return the "old" value,
          even if it means executing a lazy callable upon attribute change.

        parent_token
          Usually references the MapperProperty, used as a key for
          the hasparent() function to identify an "owning" attribute.
          Allows multiple AttributeImpls to all match a single 
          owner attribute.
          
        expire_missing
          if False, don't add an "expiry" callable to this attribute
          during state.expire_attributes(None), if no value is present 
          for this key.
          
        """
        self.class_ = class_
        self.key = key
        self.callable_ = callable_
        self.dispatch = dispatch
        self.trackparent = trackparent
        self.parent_token = parent_token or self
        if compare_function is None:
            self.is_equal = operator.eq
        else:
            self.is_equal = compare_function
        
        # TODO: pass in the manager here
        # instead of doing a lookup
        attr = manager_of_class(class_)[key]
        
        for ext in util.to_list(extension or []):
            ext._adapt_listener(attr, ext)
            
        if active_history:
            self.dispatch.active_history = True

        self.expire_missing = expire_missing
        
    def _get_active_history(self):
        """Backwards compat for impl.active_history"""
        
        return self.dispatch.active_history
    
    def _set_active_history(self, value):
        self.dispatch.active_history = value
    
    active_history = property(_get_active_history, _set_active_history)
    
        
    def hasparent(self, state, optimistic=False):
        """Return the boolean value of a `hasparent` flag attached to 
        the given state.

        The `optimistic` flag determines what the default return value
        should be if no `hasparent` flag can be located.

        As this function is used to determine if an instance is an
        *orphan*, instances that were loaded from storage should be
        assumed to not be orphans, until a True/False value for this
        flag is set.

        An instance attribute that is loaded by a callable function
        will also not have a `hasparent` flag.

        """
        return state.parents.get(id(self.parent_token), optimistic)

    def sethasparent(self, state, value):
        """Set a boolean flag on the given item corresponding to
        whether or not it is attached to a parent object via the
        attribute represented by this ``InstrumentedAttribute``.

        """
        state.parents[id(self.parent_token)] = value

    def set_callable(self, state, callable_):
        """Set a callable function for this attribute on the given object.

        This callable will be executed when the attribute is next
        accessed, and is assumed to construct part of the instances
        previously stored state. When its value or values are loaded,
        they will be established as part of the instance's *committed
        state*.  While *trackparent* information will be assembled for
        these instances, attribute-level event handlers will not be
        fired.

        The callable overrides the class level callable set in the
        ``InstrumentedAttribute`` constructor.

        """
        state.callables[self.key] = callable_

    def get_history(self, state, dict_, passive=PASSIVE_OFF):
        raise NotImplementedError()
    
    def get_all_pending(self, state, dict_):
        """Return a list of tuples of (state, obj) 
        for all objects in this attribute's current state 
        + history.
        
        Only applies to object-based attributes.

        This is an inlining of existing functionality
        which roughly correponds to:
    
            get_state_history(
                        state, 
                        key, 
                        passive=PASSIVE_NO_INITIALIZE).sum()

        """
        raise NotImplementedError()
        
    def initialize(self, state, dict_):
        """Initialize the given state's attribute with an empty value."""

        dict_[self.key] = None
        return None

    def get(self, state, dict_, passive=PASSIVE_OFF):
        """Retrieve a value from the given object.

        If a callable is assembled on this object's attribute, and
        passive is False, the callable will be executed and the
        resulting value will be set as the new value for this attribute.
        """

        if self.key in dict_:
            return dict_[self.key]
        else:
            # if history present, don't load
            key = self.key
            if key not in state.committed_state or \
                state.committed_state[key] is NEVER_SET:
                if passive is PASSIVE_NO_INITIALIZE:
                    return PASSIVE_NO_RESULT
                    
                if key in state.callables:
                    callable_ = state.callables[key]
                    value = callable_(passive)
                elif self.callable_:
                    value = self.callable_(state, passive)
                else:
                    value = ATTR_EMPTY

                if value is PASSIVE_NO_RESULT:
                    return value
                elif value is ATTR_WAS_SET:
                    try:
                        return dict_[key]
                    except KeyError:
                        # TODO: no test coverage here.
                        raise KeyError(
                                "Deferred loader for attribute "
                                "%r failed to populate "
                                "correctly" % key)
                elif value is not ATTR_EMPTY:
                    return self.set_committed_value(state, dict_, value)

            # Return a new, empty value
            return self.initialize(state, dict_)
    
    def append(self, state, dict_, value, initiator, passive=PASSIVE_OFF):
        self.set(state, dict_, value, initiator, passive=passive)

    def remove(self, state, dict_, value, initiator, passive=PASSIVE_OFF):
        self.set(state, dict_, None, initiator, passive=passive)

    def set(self, state, dict_, value, initiator, passive=PASSIVE_OFF):
        raise NotImplementedError()

    def get_committed_value(self, state, dict_, passive=PASSIVE_OFF):
        """return the unchanged value of this attribute"""

        if self.key in state.committed_state:
            value = state.committed_state[self.key]
            if value is NO_VALUE:
                return None
            else:
                return value
        else:
            return self.get(state, dict_, passive=passive)

    def set_committed_value(self, state, dict_, value):
        """set an attribute value on the given instance and 'commit' it."""

        dict_[self.key] = value
        state.commit(dict_, [self.key])
        return value

class ScalarAttributeImpl(AttributeImpl):
    """represents a scalar value-holding InstrumentedAttribute."""

    accepts_scalar_loader = True
    uses_objects = False
    supports_population = True

    def delete(self, state, dict_):

        # TODO: catch key errors, convert to attributeerror?
        if self.dispatch.active_history:
            old = self.get(state, dict_)
        else:
            old = dict_.get(self.key, NO_VALUE)

        if self.dispatch.on_remove:
            self.fire_remove_event(state, dict_, old, None)
        state.modified_event(dict_, self, old)
        del dict_[self.key]

    def get_history(self, state, dict_, passive=PASSIVE_OFF):
        return History.from_scalar_attribute(
            self, state, dict_.get(self.key, NO_VALUE))

    def set(self, state, dict_, value, initiator, passive=PASSIVE_OFF):
        if initiator and initiator.parent_token is self.parent_token:
            return

        if self.dispatch.active_history:
            old = self.get(state, dict_)
        else:
            old = dict_.get(self.key, NO_VALUE)

        if self.dispatch.on_set:
            value = self.fire_replace_event(state, dict_, 
                                                value, old, initiator)
        state.modified_event(dict_, self, old)
        dict_[self.key] = value

    def fire_replace_event(self, state, dict_, value, previous, initiator):
        for fn in self.dispatch.on_set:
            value = fn(state, value, previous, initiator or self)
        return value

    def fire_remove_event(self, state, dict_, value, initiator):
        for fn in self.dispatch.on_remove:
            fn(state, value, initiator or self)

    @property
    def type(self):
        self.property.columns[0].type


class MutableScalarAttributeImpl(ScalarAttributeImpl):
    """represents a scalar value-holding InstrumentedAttribute, which can
    detect changes within the value itself.

    """

    uses_objects = False
    supports_population = True

    def __init__(self, class_, key, callable_, dispatch,
                    class_manager, copy_function=None,
                    compare_function=None, **kwargs):
        super(ScalarAttributeImpl, self).__init__(
                                            class_, 
                                            key, 
                                            callable_, dispatch,
                                            compare_function=compare_function, 
                                            **kwargs)
        class_manager.mutable_attributes.add(key)
        if copy_function is None:
            raise sa_exc.ArgumentError(
                        "MutableScalarAttributeImpl requires a copy function")
        self.copy = copy_function

    def get_history(self, state, dict_, passive=PASSIVE_OFF):
        if not dict_:
            v = state.committed_state.get(self.key, NO_VALUE)
        else:
            v = dict_.get(self.key, NO_VALUE)
            
        return History.from_scalar_attribute(self, state, v)

    def check_mutable_modified(self, state, dict_):
        a, u, d = self.get_history(state, dict_)
        return bool(a or d)

    def get(self, state, dict_, passive=PASSIVE_OFF):
        if self.key not in state.mutable_dict:
            ret = ScalarAttributeImpl.get(self, state, dict_, passive=passive)
            if ret is not PASSIVE_NO_RESULT:
                state.mutable_dict[self.key] = ret
            return ret
        else:
            return state.mutable_dict[self.key]

    def delete(self, state, dict_):
        ScalarAttributeImpl.delete(self, state, dict_)
        state.mutable_dict.pop(self.key)

    def set(self, state, dict_, value, initiator, passive=PASSIVE_OFF):
        ScalarAttributeImpl.set(self, state, dict_, value, initiator, passive)
        state.mutable_dict[self.key] = value


class ScalarObjectAttributeImpl(ScalarAttributeImpl):
    """represents a scalar-holding InstrumentedAttribute, 
       where the target object is also instrumented.

       Adds events to delete/set operations.
       
    """

    accepts_scalar_loader = False
    uses_objects = True
    supports_population = True

    def __init__(self, class_, key, callable_, dispatch,
                    trackparent=False, extension=None, copy_function=None,
                    **kwargs):
        super(ScalarObjectAttributeImpl, self).__init__(
                                            class_, 
                                            key,
                                            callable_, dispatch, 
                                            trackparent=trackparent, 
                                            extension=extension,
                                            **kwargs)

    def delete(self, state, dict_):
        old = self.get(state, dict_)
        self.fire_remove_event(state, dict_, old, self)
        del dict_[self.key]

    def get_history(self, state, dict_, passive=PASSIVE_OFF):
        if self.key in dict_:
            return History.from_object_attribute(self, state, dict_[self.key])
        else:
            current = self.get(state, dict_, passive=passive)
            if current is PASSIVE_NO_RESULT:
                return HISTORY_BLANK
            else:
                return History.from_object_attribute(self, state, current)

    def get_all_pending(self, state, dict_):
        if self.key in dict_:
            current = dict_[self.key]
            if current is not None:
                ret = [(instance_state(current), current)]
            else:
                ret = []
                
            if self.key in state.committed_state:
                original = state.committed_state[self.key]
                if original not in (NEVER_SET, PASSIVE_NO_RESULT, None) and \
                    original is not current:
                    
                    ret.append((instance_state(original), original))
            return ret
        else:
            return []

    def set(self, state, dict_, value, initiator, passive=PASSIVE_OFF):
        """Set a value on the given InstanceState.

        `initiator` is the ``InstrumentedAttribute`` that initiated the
        ``set()`` operation and is used to control the depth of a circular
        setter operation.

        """
        if initiator and initiator.parent_token is self.parent_token:
            return

        if self.dispatch.active_history:
            old = self.get(state, dict_, passive=PASSIVE_ONLY_PERSISTENT)
        else:
            old = self.get(state, dict_, passive=PASSIVE_NO_FETCH)
        
        value = self.fire_replace_event(state, dict_, value, old, initiator)
        dict_[self.key] = value

    def fire_remove_event(self, state, dict_, value, initiator):
        if self.trackparent and value is not None:
            self.sethasparent(instance_state(value), False)
        
        for fn in self.dispatch.on_remove:
            fn(state, value, initiator or self)

        state.modified_event(dict_, self, value)

    def fire_replace_event(self, state, dict_, value, previous, initiator):
        if self.trackparent:
            if (previous is not value and
                previous is not None and
                previous is not PASSIVE_NO_RESULT):
                self.sethasparent(instance_state(previous), False)
        
        for fn in self.dispatch.on_set:
            value = fn(state, value, previous, initiator or self)

        state.modified_event(dict_, self, previous)

        if self.trackparent:
            if value is not None:
                self.sethasparent(instance_state(value), True)

        return value


class CollectionAttributeImpl(AttributeImpl):
    """A collection-holding attribute that instruments changes in membership.

    Only handles collections of instrumented objects.

    InstrumentedCollectionAttribute holds an arbitrary, user-specified
    container object (defaulting to a list) and brokers access to the
    CollectionAdapter, a "view" onto that object that presents consistent bag
    semantics to the orm layer independent of the user data implementation.

    """
    accepts_scalar_loader = False
    uses_objects = True
    supports_population = True

    def __init__(self, class_, key, callable_, dispatch,
                    typecallable=None, trackparent=False, extension=None,
                    copy_function=None, compare_function=None, **kwargs):
        super(CollectionAttributeImpl, self).__init__(
                                            class_, 
                                            key, 
                                            callable_, dispatch,
                                            trackparent=trackparent,
                                            extension=extension,
                                            compare_function=compare_function, 
                                            **kwargs)

        if copy_function is None:
            copy_function = self.__copy
        self.copy = copy_function
        self.collection_factory = typecallable

    def __copy(self, item):
        return [y for y in list(collections.collection_adapter(item))]

    def get_history(self, state, dict_, passive=PASSIVE_OFF):
        current = self.get(state, dict_, passive=passive)
        if current is PASSIVE_NO_RESULT:
            return HISTORY_BLANK
        else:
            return History.from_collection(self, state, current)

    def get_all_pending(self, state, dict_):
        if self.key not in dict_:
            return []

        current = dict_[self.key]
        current = getattr(current, '_sa_adapter')
        
        if self.key in state.committed_state:
            original = state.committed_state[self.key]
            if original is not NO_VALUE:
                current_states = [(instance_state(c), c) for c in current]
                original_states = [(instance_state(c), c) for c in original]
            
                current_set = dict(current_states)
                original_set = dict(original_states)
            
                return \
                    [(s, o) for s, o in current_states if s not in original_set] + \
                    [(s, o) for s, o in current_states if s in original_set] + \
                    [(s, o) for s, o in original_states if s not in current_set]
            
        return [(instance_state(o), o) for o in current]

        
    def fire_append_event(self, state, dict_, value, initiator):
        for fn in self.dispatch.on_append:
            value = fn(state, value, initiator or self)

        state.modified_event(dict_, self, NEVER_SET, True)

        if self.trackparent and value is not None:
            self.sethasparent(instance_state(value), True)

        return value

    def fire_pre_remove_event(self, state, dict_, initiator):
        state.modified_event(dict_, self, NEVER_SET, True)

    def fire_remove_event(self, state, dict_, value, initiator):
        if self.trackparent and value is not None:
            self.sethasparent(instance_state(value), False)

        for fn in self.dispatch.on_remove:
            fn(state, value, initiator or self)

        state.modified_event(dict_, self, NEVER_SET, True)

    def delete(self, state, dict_):
        if self.key not in dict_:
            return

        state.modified_event(dict_, self, NEVER_SET, True)

        collection = self.get_collection(state, state.dict)
        collection.clear_with_event()
        # TODO: catch key errors, convert to attributeerror?
        del dict_[self.key]

    def initialize(self, state, dict_):
        """Initialize this attribute with an empty collection."""

        _, user_data = self._initialize_collection(state)
        dict_[self.key] = user_data
        return user_data

    def _initialize_collection(self, state):
        return state.manager.initialize_collection(
            self.key, state, self.collection_factory)

    def append(self, state, dict_, value, initiator, passive=PASSIVE_OFF):
        if initiator and initiator.parent_token is self.parent_token:
            return

        collection = self.get_collection(state, dict_, passive=passive)
        if collection is PASSIVE_NO_RESULT:
            value = self.fire_append_event(state, dict_, value, initiator)
            assert self.key not in dict_, \
                    "Collection was loaded during event handling."
            state.get_pending(self.key).append(value)
        else:
            collection.append_with_event(value, initiator)

    def remove(self, state, dict_, value, initiator, passive=PASSIVE_OFF):
        if initiator and initiator.parent_token is self.parent_token:
            return

        collection = self.get_collection(state, state.dict, passive=passive)
        if collection is PASSIVE_NO_RESULT:
            self.fire_remove_event(state, dict_, value, initiator)
            assert self.key not in dict_, \
                    "Collection was loaded during event handling."
            state.get_pending(self.key).remove(value)
        else:
            collection.remove_with_event(value, initiator)

    def set(self, state, dict_, value, initiator, passive=PASSIVE_OFF):
        """Set a value on the given object.

        `initiator` is the ``InstrumentedAttribute`` that initiated the
        ``set()`` operation and is used to control the depth of a circular
        setter operation.
        """

        if initiator and initiator.parent_token is self.parent_token:
            return

        self._set_iterable(
            state, dict_, value,
            lambda adapter, i: adapter.adapt_like_to_iterable(i))

    def _set_iterable(self, state, dict_, iterable, adapter=None):
        """Set a collection value from an iterable of state-bearers.

        ``adapter`` is an optional callable invoked with a CollectionAdapter
        and the iterable.  Should return an iterable of state-bearing
        instances suitable for appending via a CollectionAdapter.  Can be used
        for, e.g., adapting an incoming dictionary into an iterator of values
        rather than keys.

        """
        # pulling a new collection first so that an adaptation exception does
        # not trigger a lazy load of the old collection.
        new_collection, user_data = self._initialize_collection(state)
        if adapter:
            new_values = list(adapter(new_collection, iterable))
        else:
            new_values = list(iterable)

        old = self.get(state, dict_, passive=PASSIVE_ONLY_PERSISTENT)
        if old is PASSIVE_NO_RESULT:
            old = self.initialize(state, dict_)
        elif old is iterable:
            # ignore re-assignment of the current collection, as happens
            # implicitly with in-place operators (foo.collection |= other)
            return

        # place a copy of "old" in state.committed_state
        state.modified_event(dict_, self, old, True)

        old_collection = getattr(old, '_sa_adapter')

        dict_[self.key] = user_data

        collections.bulk_replace(new_values, old_collection, new_collection)
        old_collection.unlink(old)

    def set_committed_value(self, state, dict_, value):
        """Set an attribute value on the given instance and 'commit' it."""

        collection, user_data = self._initialize_collection(state)

        if value:
            collection.append_multiple_without_event(value)

        state.dict[self.key] = user_data

        state.commit(dict_, [self.key])

        if self.key in state.pending:
            
            # pending items exist.  issue a modified event,
            # add/remove new items.
            state.modified_event(dict_, self, user_data, True)

            pending = state.pending.pop(self.key)
            added = pending.added_items
            removed = pending.deleted_items
            for item in added:
                collection.append_without_event(item)
            for item in removed:
                collection.remove_without_event(item)

        return user_data

    def get_collection(self, state, dict_, 
                            user_data=None, passive=PASSIVE_OFF):
        """Retrieve the CollectionAdapter associated with the given state.

        Creates a new CollectionAdapter if one does not exist.

        """
        if user_data is None:
            user_data = self.get(state, dict_, passive=passive)
            if user_data is PASSIVE_NO_RESULT:
                return user_data

        return getattr(user_data, '_sa_adapter')

def backref_listeners(attribute, key, uselist):
    """Apply listeners to synchronize a two-way relationship."""

    def set_(state, child, oldchild, initiator):
        if oldchild is child:
            return child

        if oldchild is not None and oldchild is not PASSIVE_NO_RESULT:
            # With lazy=None, there's no guarantee that the full collection is
            # present when updating via a backref.
            old_state, old_dict = instance_state(oldchild),\
                                    instance_dict(oldchild)
            impl = old_state.manager[key].impl
            try:
                impl.remove(old_state, 
                            old_dict, 
                            state.obj(), 
                            initiator, passive=PASSIVE_NO_FETCH)
            except (ValueError, KeyError, IndexError):
                pass
                
        if child is not None:
            child_state, child_dict = instance_state(child),\
                                        instance_dict(child)
            child_state.manager[key].impl.append(
                                            child_state, 
                                            child_dict, 
                                            state.obj(), 
                                            initiator, 
                                            passive=PASSIVE_NO_FETCH)
        return child

    def append(state, child, initiator):
        child_state, child_dict = instance_state(child), \
                                    instance_dict(child)
        child_state.manager[key].impl.append(
                                            child_state, 
                                            child_dict, 
                                            state.obj(), 
                                            initiator, 
                                            passive=PASSIVE_NO_FETCH)
        return child

    def remove(state, child, initiator):
        if child is not None:
            child_state, child_dict = instance_state(child),\
                                        instance_dict(child)
            child_state.manager[key].impl.remove(
                                            child_state, 
                                            child_dict, 
                                            state.obj(), 
                                            initiator,
                                            passive=PASSIVE_NO_FETCH)
    
    if uselist:
        event.listen(attribute, "on_append", append, retval=True, raw=True)
    else:
        event.listen(attribute, "on_set", set_, retval=True, raw=True)
    # TODO: need coverage in test/orm/ of remove event
    event.listen(attribute, "on_remove", remove, retval=True, raw=True)
        
class History(tuple):
    """A 3-tuple of added, unchanged and deleted values,
    representing the changes which have occured on an instrumented
    attribute.
    
    Each tuple member is an iterable sequence.

    """

    __slots__ = ()

    added = property(itemgetter(0))
    """Return the collection of items added to the attribute (the first tuple
    element)."""
    
    unchanged = property(itemgetter(1))
    """Return the collection of items that have not changed on the attribute
    (the second tuple element)."""
    
    
    deleted = property(itemgetter(2))
    """Return the collection of items that have been removed from the
    attribute (the third tuple element)."""
    
    def __new__(cls, added, unchanged, deleted):
        return tuple.__new__(cls, (added, unchanged, deleted))
    
    def __nonzero__(self):
        return self != HISTORY_BLANK
    
    def empty(self):
        """Return True if this :class:`History` has no changes
        and no existing, unchanged state.
        
        """
        
        return not bool(
                        (self.added or self.deleted)
                        or self.unchanged and self.unchanged != [None]
                    ) 
        
    def sum(self):
        """Return a collection of added + unchanged + deleted."""
        
        return (self.added or []) +\
                (self.unchanged or []) +\
                (self.deleted or [])
    
    def non_deleted(self):
        """Return a collection of added + unchanged."""
        
        return (self.added or []) +\
                (self.unchanged or [])
    
    def non_added(self):
        """Return a collection of unchanged + deleted."""
        
        return (self.unchanged or []) +\
                (self.deleted or [])
    
    def has_changes(self):
        """Return True if this :class:`History` has changes."""
        
        return bool(self.added or self.deleted)
        
    def as_state(self):
        return History(
            [(c is not None and c is not PASSIVE_NO_RESULT)
             and instance_state(c) or None
             for c in self.added],
            [(c is not None and c is not PASSIVE_NO_RESULT)
             and instance_state(c) or None
             for c in self.unchanged],
            [(c is not None and c is not PASSIVE_NO_RESULT)
             and instance_state(c) or None
             for c in self.deleted],
            )

    @classmethod
    def from_scalar_attribute(cls, attribute, state, current):
        original = state.committed_state.get(attribute.key, NEVER_SET)
        if current is NO_VALUE:
            if (original is not None and
                original is not NEVER_SET and
                original is not NO_VALUE):
                deleted = [original]
            else:
                deleted = ()
            return cls((), (), deleted)
        elif original is NO_VALUE:
            return cls([current], (), ())
        elif (original is NEVER_SET or
              attribute.is_equal(current, original) is True):
            # dont let ClauseElement expressions here trip things up
            return cls((), [current], ())
        else:
            if original is not None:
                deleted = [original]
            else:
                deleted = ()
            return cls([current], (), deleted)

    @classmethod
    def from_object_attribute(cls, attribute, state, current):
        original = state.committed_state.get(attribute.key, NEVER_SET)
        
        if current is NO_VALUE:
            if (original is not None and
                original is not NEVER_SET and
                original is not NO_VALUE):
                deleted = [original]
            else:
                deleted = ()
            return cls((), (), deleted)
        elif original is NO_VALUE:
            return cls([current], (), ())
        elif (original is NEVER_SET or
                current is original):
            return cls((), [current], ())
        else:
            if original is not None:
                deleted = [original]
            else:
                deleted = ()
            return cls([current], (), deleted)

    @classmethod
    def from_collection(cls, attribute, state, current):
        original = state.committed_state.get(attribute.key, NEVER_SET)
        current = getattr(current, '_sa_adapter')
        
        if original is NO_VALUE:
            return cls(list(current), (), ())
        elif original is NEVER_SET:
            return cls((), list(current), ())
        else:
            current_states = [(instance_state(c), c) for c in current]
            original_states = [(instance_state(c), c) for c in original]
            
            current_set = dict(current_states)
            original_set = dict(original_states)
            
            return cls(
                [o for s, o in current_states if s not in original_set],
                [o for s, o in current_states if s in original_set],
                [o for s, o in original_states if s not in current_set]
            )

HISTORY_BLANK = History(None, None, None)

def get_history(obj, key, **kwargs):
    """Return a :class:`.History` record for the given object 
    and attribute key.
    
    :param obj: an object whose class is instrumented by the
      attributes package.  
    
    :param key: string attribute name.
    
    :param kwargs: Optional keyword arguments currently
      include the ``passive`` flag, which indicates if the attribute should be
      loaded from the database if not already present (:attr:`PASSIVE_NO_FETCH`), and
      if the attribute should be not initialized to a blank value otherwise
      (:attr:`PASSIVE_NO_INITIALIZE`). Default is :attr:`PASSIVE_OFF`.
    
    """
    return get_state_history(instance_state(obj), key, **kwargs)

def get_state_history(state, key, **kwargs):
    return state.get_history(key, **kwargs)

    
def has_parent(cls, obj, key, optimistic=False):
    """TODO"""
    manager = manager_of_class(cls)
    state = instance_state(obj)
    return manager.has_parent(state, key, optimistic)

def register_attribute(class_, key, **kw):
    comparator = kw.pop('comparator', None)
    parententity = kw.pop('parententity', None)
    doc = kw.pop('doc', None)
    desc = register_descriptor(class_, key, 
                            comparator, parententity, doc=doc)
    register_attribute_impl(class_, key, **kw)
    return desc
    
def register_attribute_impl(class_, key,         
        uselist=False, callable_=None, 
        useobject=False, mutable_scalars=False, 
        impl_class=None, backref=None, **kw):
    
    manager = manager_of_class(class_)
    if uselist:
        factory = kw.pop('typecallable', None)
        typecallable = manager.instrument_collection_class(
            key, factory or list)
    else:
        typecallable = kw.pop('typecallable', None)

    dispatch = manager[key].dispatch
    
    if impl_class:
        impl = impl_class(class_, key, typecallable, dispatch, **kw)
    elif uselist:
        impl = CollectionAttributeImpl(class_, key, callable_, dispatch,
                                       typecallable=typecallable, **kw)
    elif useobject:
        impl = ScalarObjectAttributeImpl(class_, key, callable_,
                                        dispatch,**kw)
    elif mutable_scalars:
        impl = MutableScalarAttributeImpl(class_, key, callable_, dispatch,
                                          class_manager=manager, **kw)
    else:
        impl = ScalarAttributeImpl(class_, key, callable_, dispatch, **kw)

    manager[key].impl = impl
    
    if backref:
        backref_listeners(manager[key], backref, uselist)

    manager.post_configure_attribute(key)
    return manager[key]
    
def register_descriptor(class_, key, comparator=None, 
                                parententity=None, property_=None, doc=None):
    manager = manager_of_class(class_)

    descriptor = InstrumentedAttribute(class_, key, comparator=comparator,
                                            parententity=parententity)
    
    descriptor.__doc__ = doc
        
    manager.instrument_attribute(key, descriptor)
    return descriptor

def unregister_attribute(class_, key):
    manager_of_class(class_).uninstrument_attribute(key)

def init_collection(obj, key):
    """Initialize a collection attribute and return the collection adapter.
    
    This function is used to provide direct access to collection internals
    for a previously unloaded attribute.  e.g.::
        
        collection_adapter = init_collection(someobject, 'elements')
        for elem in values:
            collection_adapter.append_without_event(elem)
    
    For an easier way to do the above, see
     :func:`~sqlalchemy.orm.attributes.set_committed_value`.
    
    obj is an instrumented object instance.  An InstanceState
    is accepted directly for backwards compatibility but 
    this usage is deprecated.
    
    """
    state = instance_state(obj)
    dict_ = state.dict
    return init_state_collection(state, dict_, key)
    
def init_state_collection(state, dict_, key):
    """Initialize a collection attribute and return the collection adapter."""
    
    attr = state.manager[key].impl
    user_data = attr.initialize(state, dict_)
    return attr.get_collection(state, dict_, user_data)

def set_committed_value(instance, key, value):
    """Set the value of an attribute with no history events.
    
    Cancels any previous history present.  The value should be 
    a scalar value for scalar-holding attributes, or
    an iterable for any collection-holding attribute.

    This is the same underlying method used when a lazy loader
    fires off and loads additional data from the database.
    In particular, this method can be used by application code
    which has loaded additional attributes or collections through
    separate queries, which can then be attached to an instance
    as though it were part of its original loaded state.
    
    """
    state, dict_ = instance_state(instance), instance_dict(instance)
    state.manager[key].impl.set_committed_value(state, dict_, value)
    
def set_attribute(instance, key, value):
    """Set the value of an attribute, firing history events.
    
    This function may be used regardless of instrumentation
    applied directly to the class, i.e. no descriptors are required.
    Custom attribute management schemes will need to make usage
    of this method to establish attribute state as understood
    by SQLAlchemy.
    
    """
    state, dict_ = instance_state(instance), instance_dict(instance)
    state.manager[key].impl.set(state, dict_, value, None)

def get_attribute(instance, key):
    """Get the value of an attribute, firing any callables required.

    This function may be used regardless of instrumentation
    applied directly to the class, i.e. no descriptors are required.
    Custom attribute management schemes will need to make usage
    of this method to make usage of attribute state as understood
    by SQLAlchemy.
    
    """
    state, dict_ = instance_state(instance), instance_dict(instance)
    return state.manager[key].impl.get(state, dict_)

def del_attribute(instance, key):
    """Delete the value of an attribute, firing history events.

    This function may be used regardless of instrumentation
    applied directly to the class, i.e. no descriptors are required.
    Custom attribute management schemes will need to make usage
    of this method to establish attribute state as understood
    by SQLAlchemy.
    
    """
    state, dict_ = instance_state(instance), instance_dict(instance)
    state.manager[key].impl.delete(state, dict_)

