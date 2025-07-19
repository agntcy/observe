# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import inspect


def _is_async_method(fn):
    # check if co-routine function or async generator( example : using async & yield)
    if inspect.iscoroutinefunction(fn) or inspect.isasyncgenfunction(fn):
        return True

    # Check if this is a wrapped function that might hide the original async nature
    # Look for common wrapper attributes that might contain the original function
    for attr_name in ["__wrapped__", "func", "_func", "function"]:
        if hasattr(fn, attr_name):
            wrapped_fn = getattr(fn, attr_name)
            if wrapped_fn and callable(wrapped_fn):
                if inspect.iscoroutinefunction(
                    wrapped_fn
                ) or inspect.isasyncgenfunction(wrapped_fn):
                    return True
                # Recursively check in case of multiple levels of wrapping
                if _is_async_method(wrapped_fn):
                    return True

    return False


def _is_async_generator(fn):
    """Check if function is an async generator, looking through wrapped functions"""
    if inspect.isasyncgenfunction(fn):
        return True

    # Check if this is a wrapped function that might hide the original async generator nature
    for attr_name in ["__wrapped__", "func", "_func", "function"]:
        if hasattr(fn, attr_name):
            wrapped_fn = getattr(fn, attr_name)
            if wrapped_fn and callable(wrapped_fn):
                if inspect.isasyncgenfunction(wrapped_fn):
                    return True
                # Recursively check in case of multiple levels of wrapping
                if _is_async_generator(wrapped_fn):
                    return True

    return False


def _get_original_function_name(fn):
    """Extract the original function name from potentially wrapped functions"""
    if hasattr(fn, "__qualname__") and fn.__qualname__:
        return fn.__qualname__

    # Look for the original function in common wrapper attributes
    for attr_name in ["__wrapped__", "func", "_func", "function"]:
        if hasattr(fn, attr_name):
            wrapped_fn = getattr(fn, attr_name)
            if wrapped_fn and callable(wrapped_fn):
                if hasattr(wrapped_fn, "__qualname__") and wrapped_fn.__qualname__:
                    return wrapped_fn.__qualname__
                # Recursively check in case of multiple levels of wrapping
                result = _get_original_function_name(wrapped_fn)
                if result:
                    return result

    # Fallback to function name if qualname is not available
    return getattr(fn, "__name__", "unknown_function")
