
from get_slices import *
from get_dicts import *
import math

import sys

from dask.base import tokenize
import operator
from operator import getitem



__all__ = ("apply_clustered_strategy", "create_buffers", "create_buffer_node", 
           "update_io_tasks", "update_io_tasks_rechunk", "update_io_tasks_getitem", 
           "add_getitem_task_in_graph", "recursive_search_and_update", "convert_proxy_to_buffer_slices",
           "numeric_to_3d_pos", "_3d_to_numeric_pos", "is_in_load", "get_buffer_slices_from_original_array")


def apply_clustered_strategy(graph, slices_dict, deps_dict, array_to_original, original_array_chunks, original_array_shapes, original_array_blocks_shape):
    for proxy_array_name, slices_list in slices_dict.items(): 
        buffers = create_buffers(slices_list, proxy_array_name, array_to_original, original_array_chunks, original_array_blocks_shape)
  
        for load_index in range(len(buffers)):
            load = buffers[load_index]
            # if len(load) > 1: TODO: remettre ça, l'enlever sert juste à voir (dans la viz) si le buffering marche 
            graph, buffer_node_name = create_buffer_node(graph, proxy_array_name, load, array_to_original, original_array_blocks_shape, original_array_chunks)
            graph = update_io_tasks(graph, load, deps_dict, proxy_array_name, array_to_original, original_array_chunks, original_array_blocks_shape, buffer_node_name)
    return graph


def create_buffers(slices_list, proxy_array_name, array_to_original, original_array_chunks, original_array_blocks_shape, nb_bytes_per_val=8):
    """ current strategy : entire blocks
    # TODO support more strategies
    """

    def get_buffer_mem_size(config):
        try:
            optimization = config.get("io-optimizer")
            try:
                return config.get("io-optimizer.memory_available")
            except:
                print("missing configuration information memory_available")
                print("using default configuration: 1 gigabytes")
                return 1000000000
        except:
            raise ValueError("io-optimizer not enabled")


    def get_load_strategy(buffer_mem_size, block_shape, original_array_blocks_shape, nb_bytes_per_val):
        """ get clustered writes best load strategy given the memory available for io optimization
        """
        block_mem_size = block_shape[0] * block_shape[1] * block_shape[2] * nb_bytes_per_val
        block_row_size = block_mem_size * original_array_blocks_shape[2]
        block_slice_size = block_row_size * original_array_blocks_shape[1]

        if buffer_mem_size >= block_slice_size:
            nb_slices = math.floor(buffer_mem_size / block_slice_size)
            return "slices", nb_slices * original_array_blocks_shape[2] * original_array_blocks_shape[1]
        elif buffer_mem_size >= block_row_size:
            nb_rows = math.floor(buffer_mem_size / block_row_size)
            return "rows", nb_rows * original_array_blocks_shape[2]
        else:
            return "blocks", math.floor(buffer_mem_size / block_mem_size)


    def new_list(list_of_lists):
        list_of_lists.append(list())
        return list_of_lists, None


    def bad_configuration_incoming(prev_i, strategy, original_array_blocks_shape):
            """ to avoid bad configurations in clustered writes
            """
            if not prev_i:
                return False 
            elif strategy == "blocks" and prev_i % original_array_blocks_shape[2] == 0:
                return True 
            elif strategy == "rows" and prev_i % (original_array_blocks_shape[1] * original_array_blocks_shape[1]) == 0:
                return True 
            else:
                return False


    def test_if_create_new_load(list_of_lists, prev_i, strategy, original_array_blocks_shape):
        if len(list_of_lists[-1]) == nb_blocks_per_load:
            return new_list(list_of_lists)
        elif prev_i and next_i != prev_i + 1:
            return new_list(list_of_lists)
        elif bad_configuration_incoming(prev_i, strategy, original_array_blocks_shape):
            return new_list(list_of_lists)
        else:
            return list_of_lists, prev_i

            
    original_array_name = array_to_original[proxy_array_name]
    original_array_blocks_shape = original_array_blocks_shape[original_array_name]
    buffer_mem_size = 1000000000 # get_buffer_mem_size(config)
    block_shape = original_array_chunks[original_array_name]
    strategy, nb_blocks_per_load = get_load_strategy(buffer_mem_size, block_shape, original_array_blocks_shape, nb_bytes_per_val)

    list_of_lists, prev_i = new_list(list())
    while len(slices_list) > 0:
        next_i = slices_list.pop(0)
        list_of_lists, prev_i = test_if_create_new_load(list_of_lists, prev_i, strategy, original_array_blocks_shape)
        list_of_lists[len(list_of_lists) - 1].append(next_i)
        prev_i = next_i       

    return list_of_lists


def get_buffer_slices_from_original_array(load, shape, original_array_chunk):
    def get_coords_in_image(block_coord, original_array_chunk):
        return tuple([block_coord[i] * original_array_chunk[i] for i in range(3)])

    """_min = [None, None, None]
    _max = [None, None, None]
    for block_index_num in range(load[0], load[-1] + 1):
        block_index_3d = numeric_to_3d_pos(block_index_num, shape, order='C') 
        for j in range(3):
            if _max[j] == None:
                _max[j] = block_index_3d[j]
            if _min[j] == None:
                _min[j] = block_index_3d[j]
            if block_index_3d[j] > _max[j]:
                _max[j] = block_index_3d[j]
            if block_index_3d[j] < _min[j]:
                _min[j] = block_index_3d[j]

    start = get_coords_in_image(tuple(_min), original_array_chunk)
    end = tuple([x + 1 for x in tuple(_max)])
    end = get_coords_in_image(end, original_array_chunk)
    return (slice(start[0], end[0], None),
            slice(start[1], end[1], None),
            slice(start[2], end[2], None))"""



    start = min(load)
    end = max(load) + 1

    start = block_index_3d = numeric_to_3d_pos(start, shape, order='C') 
    end = block_index_3d = numeric_to_3d_pos(end, shape, order='C') 

    mini = [min(s, e) for s, e in zip(start, end)]
    maxi = [max(s, e) for s, e in zip(start, end)]

    mini = [e * d for e, d in zip(mini, original_array_chunk)]
    maxi = [e * d for e, d in zip(maxi, original_array_chunk)]

    return (slice(mini[0], maxi[0], None),
            slice(mini[1], maxi[1], None),
            slice(mini[2], maxi[2], None))


def create_buffer_node(dask_graph, proxy_array_name, load, array_to_original, original_array_blocks_shape, original_array_chunks):
    # get new key
    merged_array_proxy_name = 'merged-part-' + str(load[0]) + '-' + str(load[-1])
    key = (merged_array_proxy_name, 0, 0, 0)
    
    # get new value
    # array_proxy_dict = dask_graph[proxy_array_name]
    original_array_name = array_to_original[proxy_array_name]
    original_array_chunk = original_array_chunks[original_array_name]
    
    shape = original_array_blocks_shape[original_array_name]
    buffer_block_slices = get_buffer_slices_from_original_array(load, shape, original_array_chunk)
    get_func = getitem # get_func = array_proxy_dict[list(array_proxy_dict.keys())[0]][0]
    value = (get_func, original_array_name, (buffer_block_slices[0], 
                                             buffer_block_slices[1], 
                                             buffer_block_slices[2]))

    # add new key/val pair to the dask graph
    dask_graph[merged_array_proxy_name] = {key: value}
    return dask_graph, merged_array_proxy_name


def update_io_tasks(graph, load, deps_dict, proxy_array_name, array_to_original, original_array_chunks, original_array_blocks_shape, buffer_node_name):
    keys_dict = get_keys_from_graph(graph)
    dependent_tasks = deps_dict[proxy_array_name]
    
    if 'rechunk-merge' in list(keys_dict.keys()):
        rechunk_keys = keys_dict['rechunk-merge']
        for key in rechunk_keys:
            graph = update_io_tasks_rechunk(graph, key, load, dependent_tasks, buffer_node_name, array_to_original, original_array_chunks, original_array_blocks_shape)
    
    if 'getitem' in list(keys_dict.keys()):
        getitem_keys = keys_dict['getitem']
        for key in getitem_keys:
            graph[key] = update_io_tasks_getitem(graph[key], load, buffer_node_name, dependent_tasks, array_to_original, original_array_chunks, original_array_blocks_shape)   

    return graph


def update_io_tasks_rechunk(graph, rechunk_key, load, dependent_tasks, buffer_node_name, array_to_original, original_array_chunks, original_array_blocks_shape):
    def replace_rechunk_merge(graph, load, val, buffer_node_name, array_to_original, original_array_chunks, original_array_blocks_shape):
        f, concat_list = val
        graph, concat_list = recursive_search_and_update(graph, load, concat_list, buffer_node_name, array_to_original, original_array_chunks, original_array_blocks_shape)
        return graph, (f, concat_list)

    def replace_rechunk_split(val, load, buffer_node_name, array_to_original, original_array_chunks, original_array_blocks_shape):
        # extract proxy name and slices
        get_func, proxy_key, slices = val

        if is_in_load(proxy_key, load, array_to_original, original_array_blocks_shape):
            array_proxy_name = proxy_key[0]
            proxy_array_part = proxy_key[1:]
            pos_in_buffer, slices_from_buffer  = convert_proxy_to_buffer_slices(proxy_key, buffer_node_name, slices, array_to_original, original_array_chunks, original_array_blocks_shape)
            return (get_func, (buffer_node_name, 0, 0, 0), slices_from_buffer)
        else:
            return val

    rechunk_graph = graph[rechunk_key]

    for k in list(rechunk_graph.keys()):
        if k in dependent_tasks:
            key_name = k[0]
            val = rechunk_graph[k]
            if 'rechunk-merge' in key_name:
                graph, new_val = replace_rechunk_merge(graph, load, val, buffer_node_name, array_to_original, original_array_chunks, original_array_blocks_shape)
            elif 'rechunk-split' in key_name:
                new_val = replace_rechunk_split(val, load, buffer_node_name, array_to_original, original_array_chunks, original_array_blocks_shape)
            rechunk_graph[k] = new_val

    graph[rechunk_key] = rechunk_graph
    return graph


def is_in_load(proxy_key, load, array_to_original, original_array_blocks_shape):
    part = proxy_key[1:]
    proxy_name = proxy_key[0]
    if not 'array' in proxy_name:
        return False

    shape = original_array_blocks_shape[array_to_original[proxy_name]]
    num_part = _3d_to_numeric_pos(part, shape, order='C')

    if num_part in load:
        return True 
    else:
        return False


def update_io_tasks_getitem(getitem_graph, load, buffer_node_name, dependent_tasks, array_to_original, original_array_chunks, original_array_blocks_shape):
    for k in list(getitem_graph.keys()):
        if k in dependent_tasks:
            val = getitem_graph[k]
            get_func, proxy_key, slices = val
            
            if is_in_load(proxy_key, load, array_to_original, original_array_blocks_shape):
                pos_in_buffer, slices_from_buffer = convert_proxy_to_buffer_slices(proxy_key, buffer_node_name, slices, array_to_original, original_array_chunks, original_array_blocks_shape)
                new_val = (get_func, (buffer_node_name, 0, 0, 0), slices_from_buffer)
                getitem_graph[k] = new_val  
    return getitem_graph


def recursive_search_and_update(graph, load, _list, buffer_node_name, array_to_original, original_array_chunks, original_array_blocks_shape):
    if not isinstance(_list[0], tuple): # if it is not a list of targets
        for i in range(len(_list)):
            sublist = _list[i] 
            graph, sublist = recursive_search_and_update(graph, load, sublist, buffer_node_name, array_to_original, original_array_chunks, original_array_blocks_shape)
            _list[i] = sublist
    else:
        for i in range(len(_list)):
            target_key = _list[i]
            if is_in_load(target_key, load, array_to_original, original_array_blocks_shape):
                getitem_task_key, graph = add_getitem_task_in_graph(graph, buffer_node_name, target_key, array_to_original, original_array_chunks, original_array_blocks_shape)
                _list[i] = getitem_task_key
  
    return graph, _list


def add_getitem_task_in_graph(graph, buffer_node_name, proxy_key, array_to_original, original_array_chunks, original_array_blocks_shape):
    """ replace a rechunk-merged call to an array proxy part by a rechunk-merged call to a buffer proxy part 
    to do that, create a buffer proxy, add it the buffer proxy part 
    """

    # new key
    target_name = proxy_key[0]
    slices = (slice(None, None, None), slice(None, None, None), slice(None, None, None))
    buffer_proxy_name = buffer_node_name + '-proxy'
    
    # get slices from buffer_proxy
    pos_in_buffer, slices_from_buffer = convert_proxy_to_buffer_slices(proxy_key, buffer_node_name, slices, array_to_original, original_array_chunks, original_array_blocks_shape)
    buffer_proxy_subtask_key = tuple([buffer_proxy_name] + list(pos_in_buffer))
    buffer_proxy_subtask_val = (getitem, (buffer_node_name, 0, 0, 0), slices_from_buffer)

    # create buffer_proxy if does not exist
    if not buffer_proxy_name in list(graph.keys()):
        graph[buffer_proxy_name] = dict()
    
    # add to buffer proxy
    d = graph[buffer_proxy_name]
    if not buffer_proxy_subtask_key in list(d.keys()):  # it is not possible to have two keys with different values
        d[buffer_proxy_subtask_key] = buffer_proxy_subtask_val
    return buffer_proxy_subtask_key, graph


def convert_proxy_to_buffer_slices(proxy_key, buffer_proxy_name, slices, array_to_original, original_array_chunks, original_array_blocks_shape):
    """ Get the slices of the targeted block in the buffer, from the index of this block in the proxy array. 
    + apply the slices 
    """
    proxy_array_name = proxy_key[0]
    pos_in_proxy_array = proxy_key[1:]
    original_array_name = array_to_original[proxy_array_name]
    img_chunks_sizes = original_array_chunks[original_array_name]
    img_nb_blocks_per_dim = original_array_blocks_shape[original_array_name]

    split = buffer_proxy_name.split('-')
    if len(split) != 4:
        raise ValueError("expected a buffer task name")
    num_start_of_buffer = split[2]

    # convert 3d pos in image to 3d pos in buffer (merged block)
    num_pos_in_proxy = _3d_to_numeric_pos(pos_in_proxy_array, img_nb_blocks_per_dim, order='C') 
    num_pos_in_buffer = num_pos_in_proxy - int(num_start_of_buffer)
    pos_in_buffer = numeric_to_3d_pos(num_pos_in_buffer, img_nb_blocks_per_dim, order='C')

    _slice = pos_in_buffer

    start = [None] * 3
    stop = [None] * 3
    for i, sl in enumerate(slices):
        if sl.start != None:
            start[i] = (_slice[i] * img_chunks_sizes[i]) + sl.start
        else:
            start[i] = _slice[i] * img_chunks_sizes[i]

        if sl.stop != None:
            stop[i] = (_slice[i] * img_chunks_sizes[i]) + sl.stop
        else:
            stop[i] = (_slice[i] + 1) * img_chunks_sizes[i] 
            
    return pos_in_buffer, (slice(start[0], stop[0], None),
                            slice(start[1], stop[1], None),
                            slice(start[2], stop[2], None))


def numeric_to_3d_pos(numeric_pos, shape, order):
    if order == 'F':  
        nb_blocks_per_row = shape[0]
        nb_blocks_per_slice = shape[0] * shape[1]
    elif order == 'C':
        nb_blocks_per_row = shape[2]
        nb_blocks_per_slice = shape[1] * shape[2]
    else:
        raise ValueError("unsupported")
    
    i = math.floor(numeric_pos / nb_blocks_per_slice)
    numeric_pos -= i * nb_blocks_per_slice
    j = math.floor(numeric_pos / nb_blocks_per_row)
    numeric_pos -= j * nb_blocks_per_row
    k = numeric_pos
    return (i, j, k)


def _3d_to_numeric_pos(_3d_pos, shape, order):
    """
    in C order for example, should be ((_3d_pos[0]-1) * nb_blocks_per_slice) 
    but as we start at 0 we can keep (_3d_pos[0] * nb_blocks_per_slice)
    """
    if order == 'F':  
        nb_blocks_per_row = shape[0]
        nb_blocks_per_slice = shape[0] * shape[1]
    elif order == 'C':
        nb_blocks_per_row = shape[2]
        nb_blocks_per_slice = shape[1] * shape[2]
    else:
        raise ValueError("unsupported")

    return (_3d_pos[0] * nb_blocks_per_slice) + (_3d_pos[1] * nb_blocks_per_row) + _3d_pos[2] 