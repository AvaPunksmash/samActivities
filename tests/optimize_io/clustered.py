__all__ = ("apply_clustered_strategy", "create_buffers", "create_buffer_node", 
           "update_io_tasks", "update_io_tasks_rechunk", "update_io_tasks_getitem", 
           "add_getitem_task_in_graph", "recursive_search_and_update", "convert_proxy_to_buffer_slices")


def apply_clustered_strategy(graph, slices_dict, array_to_original, original_array_chunks, original_array_shapes, original_array_blocks_shape):
    for proxy_array_name, slices_list in slices_dict.items(): 
        buffers = create_buffers(slices_list, proxy_array_name)

        for load_index in range(len(buffers)):
            load = buffers[load_index]
            if len(load) > 1:
                graph, buffer_node_name = create_buffer_node(graph, proxy_array_name, load, original_array_blocks_shape, original_array_chunk)
                update_io_tasks(graph, deps_dict, proxy_array_name, original_array_chunk, original_array_blocks_shape, buffer_node_name)
    return graph


def create_buffers(slices_list, proxy_array_name, nb_bytes_per_val=8):
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


    def get_load_strategy(buffer_mem_size, nb_bytes_per_val):
        """ get clustered writes best load strategy given the memory available for io optimization
        """
        block_mem_size = block_shape[0] * block_shape[1] * block_shape[2] * nb_bytes_per_val
        block_row_size = block_mem_size * img_nb_blocks_per_dim[2]
        block_slice_size = block_row_size * img_nb_blocks_per_dim[1]

        if buffer_mem_size >= block_slice_size:
            nb_slices = math.floor(buffer_mem_size / block_slice_size)
            return "slices", nb_slices * img_nb_blocks_per_dim[2] * img_nb_blocks_per_dim[1]
        elif buffer_mem_size >= block_row_size:
            nb_rows = math.floor(buffer_mem_size / block_row_size)
            return "rows", nb_rows * img_nb_blocks_per_dim[2]
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
    buffer_mem_size = get_buffer_mem_size(config)
    strategy, nb_blocks_per_load = get_load_strategy(buffer_mem_size)

    slices_list, prev_i = new_list(list())
    while len(slices_list) > 0:
        next_i = slices_list.pop(0)
        slices_list, prev_i = test_if_create_new_load(list_of_lists, prev_i, strategy, original_array_blocks_shape)
        list_of_lists[len(list_of_lists) - 1].append(next_i)
        prev_i = next_i       

    return list_of_lists


def create_buffer_node(dask_graph, proxy_array_name, load, original_array_blocks_shape, original_array_chunk):
    def get_coords_in_image(block_coord, original_array_chunk):
        return tuple([block_coord[i] * original_array_chunk[i] for i in range(3)])

    def get_buffer_slices_from_original_array(load, original_array_blocks_shape, original_array_chunk):
        _min = [None, None, None]
        _max = [None, None, None]
        for block_index_num in range(load[0], load[-1] + 1):
            block_index_3d = numeric_to_3d_pos(block_index_num, original_array_blocks_shape, order='C') 
            for j in range(3):
                if _max[j] = None:
                    _max[j] = block_index_3d[j]
                if _min[j] = None:
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
                slice(start[2], end[2], None))
    
    # get new key
    merged_array_proxy_name = 'merged-part-' + str(load[0]) + '-' + str(load[-1])
    key = (merged_array_proxy_name, 0, 0, 0)
    
    # get new value
    array_proxy_dict = dask_graph[proxy_array_name]
    original_array_name = array_to_original[proxy_array_name]
    buffer_block_slices = get_buffer_slices_from_original_array(load, original_array_blocks_shape, original_array_chunk)
    get_func = array_proxy_dict[list(array_proxy_dict.keys())[0]][0]
    value = (get_func, original_array_name, (buffer_block_slices[0], 
                                             buffer_block_slices[1], 
                                             buffer_block_slices[2]))

    # add new key/val pair to the dask graph
    dask_graph[merged_array_proxy_name] = {key: value}
    return dask_graph, merged_array_proxy_name


def update_io_tasks(graph, deps_dict, proxy_array_name, original_array_chunk, original_array_blocks_shape, buffer_node_name):
    keys_dict = get_keys_from_graph(graph)
    rechunk_keys = keys_dict['rechunk-merge']
    getitem_keys = keys_dict['getitem']
    
    dependent_tasks = deps_dict[proxy_array_name]

    for key in rechunk_keys:
        update_io_tasks_rechunk(graph, graph[key], dependent_tasks, original_array_chunk, original_array_blocks_shape, buffer_node_name)

    for key in getitem_keys:
        update_io_tasks_getitem(graph[key], proxy_array_name, dependent_tasks)   


def update_io_tasks_rechunk(graph, rechunk_graph, dependent_tasks, original_array_chunk, original_array_blocks_shape, buffer_node_name):
    def replace_rechunk_merge(val, graph, buffer_node_name):
        f, concat_list = val
        graph, concat_list = recursive_search_and_update(graph, concat_list)
        return graph, (f, concat_list)

    def replace_rechunk_split(val, original_array_blocks_shape):
        get_func, target_key, slices = val
        _, s1, s2, s3 = target_key
        array_part_num = _3d_to_numeric_pos((s1, s2, s3), original_array_blocks_shape, order='C') 

        if not array_part_num in load:
            return val
        
        slice_of_interest = convert_proxy_to_buffer_slices(proxy_array_part, original_array_chunk, merged_task_name, original_array_blocks_shape, slices)
        return (get_func, (merged_task_name, 0, 0, 0), slice_of_interest)

    for k in list(rechunk_graph.keys()):
        if k in dependent_tasks:
            key_name = k[0]
            val = rechunk_graph[k]
            if 'rechunk-merge' in key_name:
                graph, new_val = replace_rechunk_merge(val, graph, buffer_node_name)
            elif 'rechunk-split' in key_name:
                new_val = replace_rechunk_split(val)
            rechunk_graph[k] = new_val


def update_io_tasks_getitem(getitem_graph):
    for k in list(getitem_graph.keys()):
        if k in dependent_tasks:
            val = getitem_graph[k]
            get_func, proxy_key, slices = val

            if np.all([tuple([sl.start, sl.stop]) == (None, None) for sl in slices]):
                proxy_part = proxy_key[1:]
                slice_of_interest = convert_proxy_to_buffer_slices(proxy_part, img_chunks_sizes, merged_task_name, img_nb_blocks_per_dim, None)
            else:
                raise ValueError("TODO!")
            new_val = (get_func, (merged_task_name, 0, 0, 0), slice_of_interest)
            getitem_graph[k] = new_val   


def add_getitem_task_in_graph(graph, buffer_proxy_name, array_proxy_key, slices_tuple):
    """
    buffer_proxy_name: buffer replacing array_proxy_name
    """
    new_task_name = 'buffer-proxy-' + tokenize(slices_tuple)
    new_task_key = (new_task_name, 0, 0, 0)
    slices = convert_proxy_to_buffer_slices(array_proxy_key, buffer_proxy_name, slices_tuple)
    new_task_val = (getfunc, (buffer_proxy_name, 0, 0, 0), slices)
    graph[new_task_name] = {new_task_key: new_task_val}
    return getitem_task_name, graph


def recursive_search_and_update(graph, _list):
    if not isinstance(_list[0], tuple):
        for i in range(len(_list)):
            sublist = _list[i] 
            graph, sublist = recursive_search(graph, sublist)
            _list[i] = sublist
    else:
        for i in range(len(_list)):
            target_key = _list[i]
            target_name = target_key[0]
            if 'array-' in target_name:
                getitem_task_name, graph = add_getitem_task_in_graph(graph, buffer_node_name, task_key, slices_tuple)
                _list[i] = getitem_task_name
    return graph, _list


def convert_proxy_to_buffer_slices(proxy_key, merged_task_name, slices):
    proxy_array_name = proxy_key[0]
    proxy_array_part_targeted = proxy_key[1:]
    original_array_name = array_to_original[proxy_array_name]
    img_chunks_sizes = original_array_chunks[original_array_name]
    img_nb_blocks_per_dim = original_array_blocks_shape[original_array_name]
    _, _, start_of_block, _ = merged_task_name.split('-')

    # convert 3d pos in image to 3d pos in buffer (merged block)
    num_pos = _3d_to_numeric_pos(proxy_array_part_targeted, img_nb_blocks_per_dim, order='C') # TODO à remove car on le fait deja avant
    num_pos_in_merged = num_pos - int(start_of_block)
    proxy_array_part_in_merged = numeric_to_3d_pos(num_pos_in_merged, img_nb_blocks_per_dim, order='C')

    _slice = proxy_array_part_in_merged

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
            
    return (slice(start[0], stop[0], None),
            slice(start[1], stop[1], None),
            slice(start[2], stop[2], None))