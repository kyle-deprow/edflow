'''All handy dataset classes we use.
Our Datasets (TODO usually) inherit from the `chainer.dataset.DatasetMixin`.
Please note that we overwrite the `__getitem__` method.
This does not change its functionality and there is not problem in not using
our version of `DatasetMixin`.
'''

import os
import pickle
import traceback
from zipfile import ZipFile, ZIP_DEFLATED  # , ZIP_BZIP2, ZIP_LZMA
from multiprocessing import Process, Queue

import numpy as np
from tqdm import tqdm, trange
from chainer.dataset import DatasetMixin as DatasetMixin_
# TODO maybe just pull
# https://github.com/chainer/chainer/blob/v4.4.0/chainer/dataset/dataset_mixin.py
# into the rep to avoid dependency on chainer for this one mixin - it doesnt
# even do that much and it would provide better documentation as this is
# actually our base class for datasets

from multiprocessing.managers import BaseManager
import queue

from edflow.main import traceable_method, get_implementations_from_config


class DatasetMixin(DatasetMixin_):
    def d_msg(self, val):
        '''Informs the user that val should be a dict.'''

        return 'The edflow version of DatasetMixin requires the ' \
               '`get_example` method to return a `dict`. Yours returned a ' \
               '{}'.format(type(val))

    @traceable_method(ignores=[BrokenPipeError])
    def __getitem__(self, i):
        ret_dict = super().__getitem__(i)

        if isinstance(i, slice):
            start = i.start or 0
            stop = i.stop
            step = i.step or 1
            for idx, d in zip(range(start, stop, step), ret_dict):
                if not isinstance(d, dict):
                    raise ValueError(self.d_msg(d))
                d['index_'] = idx

        elif isinstance(i, list) or isinstance(i, np.ndarray):
            for idx, d in zip(i, ret_dict):
                if not isinstance(d, dict):
                    raise ValueError(self.d_msg(d))
                d['index_'] = idx

        else:
            if not isinstance(ret_dict, dict):
                raise ValueError(self.d_msg(ret_dict))

            ret_dict['index_'] = i

        return ret_dict


def make_server_manager(port = 63127, authkey = b"edcache"):
    inqueue = queue.Queue()
    outqueue = queue.Queue()
    class InOutManager(BaseManager):
        pass
    InOutManager.register("get_inqueue", lambda: inqueue)
    InOutManager.register("get_outqueue", lambda: outqueue)
    manager = InOutManager(address=("", port), authkey = authkey)
    manager.start()
    print("Started manager server at {}".format(manager.address))
    return manager


def make_client_manager(ip, port = 63127, authkey = b"edcache"):
    class InOutManager(BaseManager):
        pass
    InOutManager.register("get_inqueue")
    InOutManager.register("get_outqueue")
    manager = InOutManager(address=(ip, port), authkey = authkey)
    manager.connect()
    print("Connected to server at {}".format(manager.address))
    return manager


def pickle_and_queue(dataset_factory,
                     inqueue,
                     outqueue,
                     naming_template='example_{}.p'):
    '''Parallelizable function to retrieve and queue examples from a Dataset.

    Args:
        dataset_factory (() -> chainer.DatasetMixin): A dataset factory, with
            methods described in :class:`CachedDataset`.
        indeces (list): List of indeces, used to retrieve samples from dataset.
        queue (mp.Queue): Queue to put the samples in.
        naming_template (str): Formatable string, which defines the name of
            the stored file given its index.
    '''
    pbar = tqdm()
    dataset = dataset_factory()
    while True:
        try:
            indices = inqueue.get_nowait()
        except queue.Empty:
            return

        for idx in indices:
            try:
                example = dataset[idx]
            except:
                print("Error getting example {}".format(idx))
                raise
            pickle_name = naming_template.format(idx)
            pickle_bytes = pickle.dumps(example)

            outqueue.put([pickle_name, pickle_bytes])
            pbar.update(1)


class _CacheDataset(DatasetMixin):
    """Only used to avoid initializing the original dataset."""
    def __init__(self, root, name):
        self.root = root
        self.name = name

        zippath = os.path.join(root, "cached", name+".zip")
        naming_template = 'example_{}.p'
        with ZipFile(zippath, 'r') as zip_f:
            zipfilenames = zip_f.namelist()
        def is_example(name):
            return name.startswith("example_") and name.endswith(".p")
        examplefilenames = [n for n in zipfilenames if is_example(n)]
        self.n = len(examplefilenames)

    def __len__(self):
        return self.n


class CachedDataset(DatasetMixin):
    """Using a Dataset of single examples creates a cached (saved to memory)
    version, which can be accessed way faster at runtime.

    To avoid creating the dataset multiple times, it is checked if the cached
    version already exists.

    Calling `__getitem__` on this class will try to retrieve the samples from
    the cached dataset to reduce the preprocessing overhead.

    The cached dataset will be stored in the root directory of the base dataset
    in the subfolder `cached` with name `name.zip`.

    Besides the usual DatasetMixin interface, datasets to be cached must
    also implement

        root        # (str) root folder to cache into
        name        # (str) unqiue name

    Optionally but highly recommended, they should provide

        in_memory_keys  # list(str) keys which will be collected from examples

    The collected values are stored in a dict of list, mapping an
    in_memory_key to a list containing the i-ths value at the i-ths place.
    This data structure is then exposed via the attribute `labels` and
    enables rapid iteration over useful labels without loading each example
    seperately. That way, downstream datasets can filter the indices of the
    cached dataset efficiently, e.g. filtering based on train/eval splits.

    Caching proceeds as follows:
    Expose a method which returns the dataset to be cached, e.g.

        def DataToCache():
          path = "/path/to/data"
          return MyCachableDataset(path)

    Start caching server on host <server_ip_or_hostname>:

        edcache --server --dataset import.path.to.DataToCache

    Wake up a worker bee on same or different hosts:

        edcache --address <server_ip_or_hostname> --dataset import.path.to.DataCache

    Start a cacherhive!
    """

    def __init__(self,
                 dataset,
                 force_cache=False,
                 keep_existing=True):
        '''Given a dataset class, stores all examples in the dataset, if this
        has not yet happened.

        Args:
            dataset (object): Dataset class which defines the following
                methods:
                    - `root`: returns the path to the raw data
                    - `name`: returns the name of the dataset -> best be unique
                    - `__len__`: number of examples in the dataset
                    - `__getitem__`: returns a sindle datum
                    - `in_memory_keys`: returns all keys, that are stored
                        alongside the dataset, in a `labels.p` file. This
                        allows to retrive labels more quickly and can be used
                        to filter the data more easily.
            force_cache (bool): If True the dataset is cached even if an
                existing, cached version is overwritten.
            keep_existing (bool): If True, existing entries in cache will
                not be recomputed and only non existing examples are
                appended to the cache. Useful if caching was interrupted.
        '''

        self.force_cache = force_cache
        self.keep_existing = keep_existing

        self.base_dataset = dataset
        self._root = root = dataset.root
        name = dataset.name

        self.store_dir = os.path.join(root, 'cached')
        self.store_path = os.path.join(self.store_dir, name + '.zip')

        #leading_zeroes = str(len(str(len(self))))
        #self.naming_template = 'example_{:0>' + leading_zeroes + '}.p'
        # above might be better, but for compatibility we need this right
        # now, because pickle_and_queue did not receive the updated template
        self.naming_template = 'example_{}.p'
        self._labels_name = 'labels.p'

        os.makedirs(self.store_dir, exist_ok=True)
        if self.force_cache:
            self.cache_dataset()

    @classmethod
    def from_cache(cls, root, name):
        """Use this constructor to avoid initialization of original dataset
        which can be useful if only the cached zip file is available or to
        avoid expensive constructors of datasets."""
        dataset = _CacheDataset(root, name)
        return cls(dataset)

    def __getstate__(self):
        """Close file before pickling."""
        if hasattr(self, "zip"):
            self.zip.close()
        self.zip = None
        self.currentpid = None
        return self.__dict__

    @property
    def fork_safe_zip(self):
        currentpid = os.getpid()
        if getattr(self, "_initpid", None) != currentpid:
            self._initpid = currentpid
            self.zip = ZipFile(self.store_path, 'r')
        return self.zip

    def cache_dataset(self):
        '''Checks if a dataset is stored. If not iterates over all possible
        indeces and stores the examples in a file, as well as the labels.'''

        if not os.path.isfile(self.store_path) or self.force_cache:
            print("Caching {}".format(self.store_path))
            manager = make_server_manager()
            inqueue = manager.get_inqueue()
            outqueue = manager.get_outqueue()

            N_examples = len(self.base_dataset)
            indeces = np.arange(N_examples)
            if self.keep_existing and os.path.isfile(self.store_path):
                with ZipFile(self.store_path, 'r') as zip_f:
                    zipfilenames = zip_f.namelist()
                zipfilenames = set(zipfilenames)
                indeces = [i for i in indeces if
                        not self.naming_template.format(i) in zipfilenames]
                print("Keeping {} cached examples.".format(N_examples - len(indeces)))
                N_examples = len(indeces)
            print("Caching {} examples.".format(N_examples))
            chunk_size = 64
            index_chunks = [indeces[i:i+chunk_size]
                    for i in range(0, len(indeces), chunk_size)]
            for chunk in index_chunks:
                inqueue.put(chunk)
            print("Waiting for results.")

            pbar = tqdm(total=N_examples)
            mode = "a" if self.keep_existing else "w"
            with ZipFile(self.store_path, mode, ZIP_DEFLATED) as self.zip:
                done_count = 0
                while True:
                    if done_count == N_examples:
                        break
                    pickle_name, pickle_bytes = outqueue.get()
                    self.zip.writestr(pickle_name, pickle_bytes)
                    pbar.update(1)
                    done_count += 1

            # after everything is done, we store memory keys seperately for
            # more efficient access
            # Note that this is always called, in case one wants to add labels
            # after caching has finished. This will add a new file with the
            # same name to the zip and it is currently not possible to delete
            # the old one. Preliminary tests have shown that the read method
            # returns the newest file if multiple ones are available but this
            # is _not_ documented or guaranteed in the API. If you experience
            # problems, try to write a new zip file with desired contents or
            # delete cached zip and cache again.
            memory_dict = dict()
            if hasattr(self.base_dataset, 'in_memory_keys'):
                print('Caching Labels.')
                memory_keys = self.base_dataset.in_memory_keys
                for key in memory_keys:
                    memory_dict[key] = list()

                for idx in trange(len(self.base_dataset)):
                    example = self[idx] # load cached version
                    # extract keys
                    for key in memory_keys:
                        memory_dict[key].append(example[key])

            with ZipFile(self.store_path, "a", ZIP_DEFLATED) as zipfile:
                zipfile.writestr(self._labels_name,
                        pickle.dumps(memory_dict))
            print("Finished caching.")

    def __len__(self):
        '''Number of examples in this Dataset.'''
        return len(self.base_dataset)

    @property
    def labels(self):
        '''Returns the labels associated with the base dataset, but from the
        cached source.'''
        if not hasattr(self, "_labels"):
            labels = self.fork_safe_zip.read(self._labels_name)
            labels = pickle.loads(labels)
            self._labels = labels
        return self._labels

    @property
    def root(self):
        '''Returns the root to the base dataset.'''
        return self._root

    def get_example(self, i):
        '''Given an index i, returns a example.'''

        example_name = self.naming_template.format(i)
        example_file = self.fork_safe_zip.read(example_name)

        example = pickle.loads(example_file)

        return example


class PathCachedDataset(CachedDataset):
    """Used for simplified decorator interface to dataset caching."""
    def __init__(self,
                 dataset,
                 path):
        self.force_cache = False
        self.keep_existing = True

        self.base_dataset = dataset
        self.store_dir = os.path.split(path)[0]
        self.store_path = path

        self.naming_template = 'example_{}.p'
        self._labels_name = 'labels.p'

        os.makedirs(self.store_dir, exist_ok=True)

        self.lenfile = self.store_path + ".p"
        if not os.path.exists(self.lenfile):
            self.force_cache = True
        self.cache_dataset()
        if not os.path.exists(self.lenfile):
            with open(self.lenfile, "wb") as f:
                pickle.dump(len(self.base_dataset), f)

    def __len__(self):
        if not (self.base_dataset is None or os.path.exists(self.lenfile)):
            return len(self.base_dataset)
        if not hasattr(self, "_len"):
            with open(self.lenfile, "rb") as f:
                self._len = pickle.load(f)
        return self._len


def cachable(path):
    """Decorator to cache datasets. If not cached, will start a caching server,
    subsequent calls will just load from cache. Currently all worker must be
    able to see the path. Be careful, function parameters are ignored on
    furture calls.
    Can be used on any callable that returns a dataset. Currently the path
    should be the path to a zip file to cache into - i.e. it should end in zip.
    """
    def decorator(fn):
        def wrapped(*args, **kwargs):
            if os.path.exists(path + ".p"):
                # cached version ready
                return PathCachedDataset(None, path)
            elif os.path.exists(path + "parameters.p"):
                # zip exists but not pickle with length - caching server
                # started and we are a worker bee
                with open(path + "parameters.p", "rb") as f:
                    args, kwargs = pickle.load(f)
                return fn(*args, **kwargs)
            else:
                # start caching server
                dataset = fn(*args, **kwargs)
                os.makedirs(os.path.split(path)[0], exist_ok=True)
                with open(path + "parameters.p", "wb") as f:
                    pickle.dump((args, kwargs), f)
                return PathCachedDataset(dataset, path)
        return wrapped
    return decorator


class SubDataset(DatasetMixin):
    """A subset of a given dataset."""
    def __init__(self, data, subindices):
        self.data = data
        self.subindices = subindices
        try:
            len(self.subindices)
        except TypeError:
            print("Expected a list of subindices.")
            raise

    def get_example(self, i):
        """Get example and process. Wrapped to make sure stacktrace is
        printed in case something goes wrong and we are in a
        MultiprocessIterator."""
        return self.data[self.subindices[i]]

    def __len__(self):
        return len(self.subindices)

    @property
    def labels(self):
        # relay if data is cached
        if not hasattr(self, "_labels"):
            self._labels = dict()
            labels = self.data.labels
            for k in labels:
                self._labels[k] = [labels[k][i] for i in self.subindices]
        return self._labels


class LabelDataset(DatasetMixin):
    """A label only dataset to avoid loading unnecessary data."""
    def __init__(self, data):
        self.data = data
        self.keys = sorted(self.data.labels.keys())

    def get_example(self, i):
        """Return labels of example."""
        example = dict((k, self.data.labels[k][i]) for k in self.keys)
        example["base_index_"] = i
        return example

    def __len__(self):
        return len(self.data)

    @property
    def labels(self):
        # relay if data is cached
        return self.data.labels


class ProcessedDataset(DatasetMixin):
    """A dataset with data processing applied."""
    def __init__(self, data, process, update = True):
        self.data = data
        self.process = process
        self.update = update

    def get_example(self, i):
        """Get example and process. Wrapped to make sure stacktrace is
        printed in case something goes wrong and we are in a
        MultiprocessIterator."""
        d = self.data.get_example(i)
        p = self.process(**d)
        if self.update:
            d.update(p)
            return d
        else:
            return p

    def __len__(self):
        return len(self.data)

    @property
    def labels(self):
        # relay if data is cached
        return self.data.labels


class ExtraLabelsDataset(DatasetMixin):
    """A dataset with extra labels added."""
    def __init__(self, data, labeler):
        self.data = data
        self._labeler = labeler
        self._new_keys = sorted(self._labeler(self.data, 0).keys())
        self._new_labels = dict()
        for k in self._new_keys:
            self._new_labels[k] = [None for _ in range(len(self.data))]
        for i in range(len(self.data)):
            new_labels = self._labeler(self.data, i)
            for k in self._new_keys:
                self._new_labels[k][i] = new_labels[k]
        self._labels = dict(self.data.labels)
        self._labels.update(self._new_labels)

    def get_example(self, i):
        """Get example and add new labels."""
        d = self.data.get_example(i)
        new_labels = dict((k, self._new_labels[k][i]) for k in self._new_labels)
        d.update(new_labels)
        return d

    def __len__(self):
        return len(self.data)

    @property
    def labels(self):
        return self._labels


class ConcatenatedDataset(DatasetMixin):
    """A dataset which concatenates given datasets."""
    def __init__(self, *datasets, balanced = False):
        self.datasets = list(datasets)
        self.lengths = [len(d) for d in self.datasets]
        self.boundaries = np.cumsum(self.lengths)
        self.balanced = balanced
        if self.balanced:
            max_length = np.max(self.lengths)
            for data_idx in range(len(self.datasets)):
                data_length = len(self.datasets[data_idx])
                if data_length != max_length:
                    cycle_indices = [i % data_length for i in range(max_length)]
                    self.datasets[data_idx] = SubDataset(
                            self.datasets[data_idx], cycle_indices)
        self.lengths = [len(d) for d in self.datasets]
        self.boundaries = np.cumsum(self.lengths)


    def get_example(self, i):
        """Get example and add dataset index to it."""
        did = np.where(i < self.boundaries)[0][0]
        if did > 0:
            local_i = i - self.boundaries[did-1]
        else:
            local_i = i
        example = self.datasets[did][local_i]
        example["dataset_index_"] = did
        return example

    def __len__(self):
        return sum(self.lengths)

    @property
    def labels(self):
        # relay if data is cached
        if not hasattr(self, "_labels"):
            labels = self.datasets[0].labels
            for i in range(1, len(self.datasets)):
                new_labels = self.datasets[i].labels
                for k in labels:
                    labels[k] += new_labels[k]
            self._labels = labels
        return self._labels


class ExampleConcatenatedDataset(DatasetMixin):
    '''Concatenates a list of datasets along the example axis.
    E.g.:
        dset1 = [{'a': 1, 'b': 2}, {'a': 3, 'b': 4}, ...]
        dset2 = [{'a': 6, 'b': 7}, {'a': 8, 'b': 9}, ...]

        dset_conc = ExampleConcatenatedDataset(dset1, dset2)
        print(dset_conc[0])
        # {'a_0': 1, 'a_1': 6,
        #  'b_0': 2, 'b_1': 7,
        #  'a': ['a_0', 'a_1'],
        #  'b': ['b_0', 'b_1']}

    The new keys (e.g. `a_0, a_1`) are numbered in the order they are taken
    from datasets. Additionally, the original, shared key is preserved and
    returns the list of newly generated keys in correct order.
    '''

    def __init__(self, *datasets):
        '''Args:
            *datasets (DatasetMixin): All the datasets to concatenate. Each
                dataset must return a dict as example!
        '''
        assert np.all(np.equal(len(datasets[0]), [len(d) for d in datasets]))
        self.datasets = datasets
        self.set_example_pars()

    def set_example_pars(self, start=None, stop=None, step=None):
        '''Allows to manipulate the length and step of the returned example
        lists.'''

        self.example_slice = slice(start, stop, step)

    def __len__(self):
        return len(self.datasets[0])

    @property
    def labels(self):
        '''Now each index corresponds to a sequence of labels.'''
        if not hasattr(self, '_labels'):
            self._labels = dict()
            for idx, dataset in self.datasets:
                for k in dataset.labels:
                    if k in self._labels:
                        self._labels[k] += [dataset.labels[k]]
                    else:
                        self._labels[k] = [dataset.labels[k]]
        return self._labels

    def get_example(self, i):
        examples = [d[i] for d in self.datasets[self.example_slice]]

        new_examples = {}
        for idx, ex in enumerate(examples):
            for key, value in ex.items():
                if key in new_examples:
                    new_examples[key] += [value]
                else:
                    new_examples[key] = [value]

        return new_examples


class SequenceDataset(DatasetMixin):
    '''Wraps around a dataset and returns sequences of examples.
    Given the length of those sequences the number of available examples
    is reduced by this length times the step taken. Additionally each
    example must have a frame id `fid`, by which it can be filtered. This is to
    ensure that each frame is taken from the same video.'''

    def __init__(self, dataset, length, step=1):
        '''Args:
            dataset (DatasetMixin): Dataset from which single frame examles
                are taken.
            length (int): Length of the returned sequences in frames.
            step (int): Step between returned frames. Must be `>= 1`.

        This dataset will have `len(dataset) - length * step` examples.
        '''

        self.step = step
        self.length = length

        frame_ids = dataset.labels['fid']
        top_indeces = np.where(np.array(frame_ids) >= (length * step - 1))[0]

        all_subdatasets = []
        for i in range(length * step):
            indeces = top_indeces - i
            subdset = SubDataset(dataset, indeces)
            all_subdatasets += [subdset]

        all_subdatasets = all_subdatasets[::-1]

        self.dset = ExampleConcatenatedDataset(*all_subdatasets)
        self.dset.set_example_pars(step=self.step)

    @property
    def labels(self):
        return self.dset.labels

    def __len__(self):
        return len(self.dset)

    def get_example(self, i):
        '''Retreives a list of examples starting at i.'''

        return self.dset[i]


def getSeqDataset(config):
    '''This allows to not define a dataset class, but use a baseclass and a
    `length` and `step` parameter in the supplied `config` to load and
    sequentialize a dataset.

    A config passed to edflow would the look like this:

    .. codeblock:: yaml

        dataset: edflow.data.dataset.getSeqDataSet
        model: Some Model
        iterator: Some Iterator

        seqdataset:
                dataset: import.path.to.your.basedataset,
                length: 3,
                step: 1}

    ``getSeqDataSet`` will import the base ``dataset`` and pass it to
    :class:`SequenceDataset` together with ``length`` and ``step`` to
    make the actually used dataset.

    Args:
        config (dict): An edflow config, with at least the keys
            ``seqdataset`` and nested inside it ``dataset``, ``seq_length`` and
            ``seq_step``.

    Returns:
        :class:`SequenceDataset`: A Sequence Dataset based on the basedataset.
    '''

    ks = 'seqdataset'
    base_dset = get_implementations_from_config(config[ks],
                                                ['dataset'])['dataset']
    base_dset = base_dset(config=config)

    return SequenceDataset(base_dset,
                           config[ks]['seq_length'],
                           config[ks]['seq_step'])


def JoinedDataset(dataset, key, n_joins):
    """Concat n_joins random samples based on the condition that
    example_i[key] == example_j[key] for all i,j. Key must be in labels of
    dataset."""
    labels = np.asarray(dataset.labels[key])
    unique_labels = np.unique(labels)
    index_map = dict()
    for value in unique_labels:
        index_map[value] = np.nonzero(labels == value)[0]
    join_indices = [list(range(len(dataset)))] # example_0 is original example
    prng = np.random.RandomState(1)
    for k in range(n_joins - 1):
        indices = [prng.choice(index_map[value]) for value in labels]
        join_indices.append(indices)
    datasets = [SubDataset(dataset, indices) for indices in join_indices]
    dataset = ExampleConcatenatedDataset(*datasets)

    return dataset


def getDebugDataset(config):
    '''Loads a dataset from the config and makes ist reasonably small.
    The config syntax works as in :function:`getSeqDataset`. See there for
    more extensive documentation.

    Args:
        config (dict): An edflow config, with at least the keys
            ``debugdataset`` and nested inside it ``dataset``,
            ``debug_length``, defining the basedataset and its size.

    Returns:
        :class:`SubDataset`: A dataset based on the basedataset of the specifed
            length.
    '''

    ks = 'debugdataset'
    base_dset = get_implementations_from_config(config[ks],
                                                ['dataset'])['dataset']
    base_dset = base_dset(config=config)
    indices = np.arange(config[ks]['debug_length'])

    return SubDataset(base_dset, indices)


class RandomlyJoinedDataset(DatasetMixin):
    '''Joins similiar JoinedDataset but randomly selects from possible joins.
    '''
    def __init__(self, dataset, key, n_joins):
        '''Args:
            dataset: Dataset to join in.
            key: Key to join on. Must be in dataset labels.
            n_joins: Number of examples to join.
        '''
        self.dataset = dataset
        self.key = key
        self.n_joins = n_joins

        labels = np.asarray(dataset.labels[key])
        unique_labels = np.unique(labels)
        self.index_map = dict()
        for value in unique_labels:
            self.index_map[value] = np.nonzero(labels == value)[0]


    def __len__(self):
        return len(self.dataset)


    @property
    def labels(self):
        '''Careful this can only give labels of the original item, not the
        joined ones.'''
        return self.dataset.labels


    def get_example(self, i):
        example = self.dataset[i]
        join_value = example[self.key]

        choices = [idx for idx in self.index_map[join_value] if not idx == i]
        join_indices = self.prng.choice(choices, self.n_joins - 1, replace = False)

        examples = [example] + [self.dataset[idx] for idx in join_indices]

        new_examples = {}
        for ex in examples:
            for key, value in ex.items():
                if key in new_examples:
                    new_examples[key] += [value]
                else:
                    new_examples[key] = [value]

        return new_examples


    @property
    def prng(self):
        currentpid = os.getpid()
        if getattr(self, "_initpid", None) != currentpid:
            self._initpid = currentpid
            self._prng = np.random.RandomState()
        return self._prng


class DataFolder(DatasetMixin):
    '''Given the root of a possibly nested folder containing datafiles and a
    Callable that generates the labels to the datafile from its full name, this
    class creates a labeled dataset.

    A filtering of unwanted Data can be achieved by having the ``label_fn``
    return ``None`` for those specific files. The actual files are only
    read when ``__getitem__`` is called.

    If for example ``label_fn`` returns a dict with the keys ``['a', 'b',
    'c']`` and ``read_fn`` returns one with keys ``['d', 'e']`` then the dict
    returned by ``__getitem__`` will contain the keys ``['a', 'b', 'c', 'd',
    'e', 'file_path_', 'index_']``.
    '''

    def __init__(self,
                 image_root,
                 read_fn,
                 label_fn,
                 sort_keys=None,
                 in_memory_keys=None):
        '''Args:
            image_root (str): Root containing the files of interest.
            read_fn (Callable): Given the path to a file, returns the datum as
                a dict.
            label_fn (Callable): Given the path to a file, returns a dict of
                labels. If ``label_fn`` returns ``None``, this file is ignored.
            sort_keys (list): A hierarchy of keys by which the data in this
                Dataset are sorted.
            in_memory_keys (list): keys which will be collected from examples
                when the dataset is cached.
        '''

        self.root = image_root
        self.read = read_fn
        self.label_fn = label_fn
        self.sort_keys = sort_keys

        if in_memory_keys is not None:
            assert isinstance(in_memory_keys, list)
            self.in_memory_keys = in_memory_keys

        self._read_labels()

    def _read_labels(self):
        import operator

        self.data = []
        for root, dirs, files in os.walk(self.root):
            for f in files:
                path = os.path.join(root, f)
                labels = self.label_fn(path)

                if labels is not None:
                    datum = {'file_path_': path}
                    datum.update(labels)
                    self.data += [datum]

        if self.sort_keys is not None:
            self.data.sort(key=operator.itemgetter(*self.sort_keys))

    def get_example(self, i):
        datum = self.data[i]
        path = datum['file_path_']

        file_content = self.read(path)
        datum.update(file_content)

        return datum

    def __len__(self):
        return len(self.data)


if __name__ == '__main__':
    import matplotlib.pyplot as plt

    r = '/home/johannes/Documents/Uni HD/Dr_J/Projects/data_creation/' \
        'show_vids/cut_selection/fortnite/'

    def rfn(im_path):
        return {'image': plt.imread(im_path)}

    def lfn(path):
        if os.path.isfile(path) and path[-4:] == '.jpg':
            fname = os.path.basename(path)
            labels = fname[:-4].split('_')
            if len(labels) == 3:
                pid, act, fid = labels
                beam = False
            else:
                pid, act, _, fid = labels
                beam = True
            return {'pid': int(pid), 'vid': 0, 'fid': int(fid), 'action': act}

    D = DataFolder(r,
                   rfn,
                   lfn,
                   ['pid', 'vid', 'fid'])

    for i in range(10):
        d = D[i]
        print(',\n '.join(['{}: {}'.format(k, v if not hasattr(v, 'shape')
                                           else v.shape)
                                           for k, v in d.items()]))
