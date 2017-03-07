"""
Contact map analysis.
"""
# Maintainer: David W.H. Swenson (dwhs@hyperblazer.net)
# Licensed under LGPL, version 2.1 or greater
import collections
import itertools
import scipy
import pandas as pd
import mdtraj as md
import pickle
import numpy as np
import warnings
import matplotlib.pyplot as plt

# TODO:
# * switch to something where you can define the haystack -- the trick is to
#   replace the current mdtraj._compute_neighbors with something that
#   build a voxel list for the haystack, and then checks the voxel for each
#   query atom. Doesn't look like anything is doing that now: neighbors
#   doesn't use voxels, neighborlist doesn't limit the haystack
# * (dream) parallelization: map-reduce like himach should work great for
#   this

def residue_neighborhood(residue, n=1):
    """Find n nearest neighbor residues

    Parameters
    ----------
    residue : mdtraj.Residue
        this residue
    n : positive int
        number of neighbors to find

    Returns
    -------
    list of int
        neighbor residue numbers
    """
    neighborhood = set([residue.index+i for i in range(-n, n+1)])
    chain = set([res.index for res in residue.chain.residues])
    # we could probably choose an faster approach here, but this is pretty
    # good, and it only gets run once per residue
    return [idx for idx in neighborhood if idx in chain]

class NearestAtoms(object):
    def __init__(self, trajectory, cutoff, frame_number=0):
        # TODO: add support for a subset of all atoms with `atoms`
        self.cutoff = cutoff
        self.frame_number = frame_number
        neighborlist = md.compute_neighborlist(trajectory, self.cutoff,
                                               self.frame_number)
        frame = trajectory[frame_number]
        self.nearest = {}
        self.nearest_distance = {}
        for (atom, neighbors) in enumerate(neighborlist):
            pairs = itertools.product([atom], neighbors)
            distances = md.compute_distances(frame, pairs)[0]  # 0th frame
            nearest = sorted(zip(distances, neighbors))[0]
            self.nearest[atom] = nearest[1]
            self.nearest_distance[atom] = nearest[0]

    @property
    def sorted_distances(self):
        listed = [(atom, self.nearest[atom], dist)
                  for (atom, dist) in list(self.nearest_distance.items())]
        return sorted(listed, key=lambda tup: tup[2])

class MinimumDistanceCounter(object):
    # count how many times each atom pair has minimum distance
    def __init__(self, trajectory, query, haystack, cutoff=0.45):
        self.atom_pairs = list(itertools.product(query, haystack))
        distances = md.compute_distances(trajectory,
                                         atom_pairs=self.atom_pairs)
        self._min_pairs = distances.argmin(axis=1)
        self.minimum_distances = distances.min(axis=1)
        self.topology = trajectory.topology
        self.cutoff = cutoff

    def _remap(self, pair_number):
        pair = self.atom_pairs[pair_number]
        return (self.topology.atom(pair[0]), self.topology.atom(pair[1]))

    @property
    def atom_history(self):
        return [self._remap(k) for k in self._min_pairs]

    @property
    def atom_count(self):
        return collections.Counter(self.atom_history)

    @property
    def residue_history(self):
        return [(a[0].residue, a[1].residue) for a in self.atom_history]

    @property
    def residue_count(self):
        return collections.Counter(self.residue_history)

class ContactCount(object):
    def __init__(self, counter, object_f, n_x, n_y):
        self._counter = counter
        self._object_f = object_f
        self.n_x = n_x
        self.n_y = n_y

    @property
    def counter(self):
        return self._counter

    @property
    def sparse_matrix(self):
        mtx = scipy.sparse.dok_matrix((self.n_x, self.n_y))
        for (k, v) in self._counter.items():
            key = list(k)
            mtx[key[0], key[1]] = v
            mtx[key[1], key[0]] = v
        return mtx

    @property
    def df(self):
        return pd.SparseDataFrame(self.sparse_matrix.todense())

    def most_common(self, obj=None):
        if obj is None:
            result = [
                ([self._object_f(idx) for idx in common[0]], common[1])
                for common in self.most_common_idx()
            ]
        else:
            obj_idx = obj.index
            result = [
                ([self._object_f(idx) for idx in common[0]], common[1])
                for common in self.most_common_idx()
                if obj_idx in common[0]
            ]
        return result

    def most_common_idx(self):
        return self._counter.most_common()


class ContactObject(object):
    """
    Generic object for contact map related analysis. Effectively abstract.

    Much of what we need to do the contact map analysis is the same for all
    analyses. It's in here.
    """
    def __init__(self, topology, query, haystack, cutoff, n_neighbors_ignored):
        # all inits required: no defaults for abstract class!
        self._topology = topology
        if query is None:
            query = topology.select("not water and not symbol == 'H'")
        if haystack is None:
            haystack = topology.select("not water and not symbol == 'H'")
        # make things private and accessible through read-only properties so
        # they don't get accidentally changed after analysis
        self._cutoff = cutoff
        self._query = set(query)
        self._haystack = set(haystack)
        self._n_neighbors_ignored = n_neighbors_ignored
        self._atom_idx_to_residue_idx = {atom.index: atom.residue.index
                                         for atom in self.topology.atoms}

    def save_to_file(self, filename, mode="w"):
        """Save this object to the given file.

        Parameters
        ----------
        filename : string
            the file to write to
        mode : 'w' or 'a'
            file writing mode. Use 'w' to overwrite, 'a' to append.
        """
        f = open(filename, mode)
        pickle.dump(self, f)
        f.close()

    @classmethod
    def from_file(cls, filename):
        f = open(filename, "r")
        return pickle.load(f)

    def __sub__(self, other):
        return ContactDifference(positive=self, negative=other)

    @property
    def cutoff(self):
        return self._cutoff

    @property
    def n_neighbors_ignored(self):
        return self._n_neighbors_ignored

    @property
    def query(self):
        return list(self._query)

    @property
    def haystack(self):
        return list(self._haystack)

    @property
    def topology(self):
        return self._topology

    @property
    def residue_query_atom_idxs(self):
        result = {}
        for atom_idx in self._query:
            residue_idx = self.topology.atom(atom_idx).residue.index
            try:
                result[residue_idx] += [atom_idx]
            except KeyError:
                result[residue_idx] = [atom_idx]
        return result


    @property
    def residue_ignore_atom_idxs(self):
        result = {}
        for residue_idx in self.residue_query_atom_idxs.keys():
            residue = self.topology.residue(residue_idx)
            # Several steps to go residue indices -> atom indices
            ignore_residue_idxs = residue_neighborhood(
                residue,
                self._n_neighbors_ignored
            )
            ignore_residues = [self.topology.residue(idx)
                               for idx in ignore_residue_idxs]
            ignore_atoms = sum([list(res.atoms)
                                for res in ignore_residues], [])
            ignore_atom_idxs = set([atom.index for atom in ignore_atoms])
            result[residue_idx] = ignore_atom_idxs
        return result

    def most_common_atoms_for_residue(self, residue):
        try:
            residue_idx = residue.index
        except AttributeError:
            residue_idx = residue
            residue = self.topology.residue(residue_idx)

        residue_atoms = set(atom.index for atom in residue.atoms)
        results = []
        for contact in self.atom_contacts.most_common_idx():
            atoms = contact[0]
            number = contact[1]
            for atom in atoms:
                if atom in residue_atoms:
                    results.append(([self.topology.atom(a) for a in atoms],
                                    number))
        return results

    def most_common_atoms_for_contact(self, contact_pair):
        contact_pair = frozenset(contact_pair)
        res_A = list(contact_pair)[0]
        res_B = list(contact_pair)[1]
        try:
            res_A_idx = res_A.index
        except AttributeError:
            res_A_idx = res_A
            res_A = self.topology.residue(res_A_idx)
        try:
            res_B_idx = res_B.index
        except AttributeError:
            resB_idx = res_B
            res_B = self.topology.residue(res_B_idx)
        atom_idxs_A = set(atom.index for atom in res_A.atoms)
        atom_idxs_B = set(atom.index for atom in res_B.atoms)
        all_atom_pairs = [
            frozenset(pair)
            for pair in itertools.product(atom_idxs_A, atom_idxs_B)
        ]
        result = [([self.topology.atom(idx) for idx in contact[0]], contact[1])
                  for contact in self.atom_contacts.most_common_idx()
                  if frozenset(contact[0]) in all_atom_pairs]
        return result


    def contact_map(self, trajectory, frame_number, residue_query_atom_idxs,
                    residue_ignore_atom_idxs):
        """
        Returns atom and residue contact maps for the given frame.

        Parameters
        ----------
        frame : mdtraj.Trajectory
            the desired frame (uses the first frame in this trajectory)
        residue_query_atom_idxs : dict
        residue_ignore_atom_idxs : dict

        Returns
        -------
        atom_contacts : collections.Counter
        residue_contact : collections.Counter
        """
        neighborlist = md.compute_neighborlist(trajectory, self.cutoff,
                                               frame_number)
        contact_pairs = set([])
        residue_pairs = set([])
        for residue_idx in residue_query_atom_idxs:
            ignore_atom_idxs = set(residue_ignore_atom_idxs[residue_idx])
            query_idxs = residue_query_atom_idxs[residue_idx]
            for atom_idx in query_idxs:
                # sets should make this fast, esp since neighbor_idxs
                # should be small and s-t is avg cost len(s)
                neighbor_idxs = set(neighborlist[atom_idx])
                contact_neighbors = neighbor_idxs - ignore_atom_idxs
                contact_neighbors = contact_neighbors & self._haystack
                # frozenset is unique key independent of order
                # local_pairs = set(frozenset((atom_idx, neighb))
                                  # for neighb in contact_neighbors)
                local_pairs = set(map(
                    frozenset,
                    itertools.product([atom_idx], contact_neighbors)
                ))
                contact_pairs |= local_pairs
                # contact_pairs |= set(frozenset((atom_idx, neighb))
                                     # for neighb in contact_neighbors)
                local_residue_partners = set(self._atom_idx_to_residue_idx[a]
                                             for a in contact_neighbors)
                local_res_pairs = set(map(
                    frozenset,
                    itertools.product([residue_idx], local_residue_partners)
                ))
                residue_pairs |= local_res_pairs

        atom_contacts = collections.Counter(contact_pairs)
        # residue_pairs = set(
            # frozenset(self._atom_idx_to_residue_idx[aa] for aa in pair)
            # for pair in contact_pairs
        # )
        residue_contacts = collections.Counter(residue_pairs)
        return (atom_contacts, residue_contacts)

    @property
    def atom_contacts(self):
        n_atoms = self.topology.n_atoms
        return ContactCount(self._atom_contacts, self.topology.atom,
                            n_atoms, n_atoms)

    @property
    def residue_contacts(self):
        n_res = self.topology.n_residues
        return ContactCount(self._residue_contacts, self.topology.residue,
                            n_res, n_res)


class ContactMap(ContactObject):
    """
    Contact map (atomic and residue) for a single frame.
    """
    def __init__(self, frame, query=None, haystack=None, cutoff=0.45,
                 n_neighbors_ignored=2):
        self._frame = frame
        super(ContactMap, self).__init__(frame.topology, query, haystack,
                                         cutoff, n_neighbors_ignored)
        contact_maps = self.contact_map(frame, 0,
                                        self.residue_query_atom_idxs,
                                        self.residue_ignore_atom_idxs)
        (self._atom_contacts, self._residue_contacts) = contact_maps


class ContactTrajectory(ContactObject):
    """
    Contact map (atomic and residue) for each individual trajectory frame.

    NOT YET IMPLEMENTED. I'm not sure whether this gives appreciable speed
    improvements over running contact map over and over.
    """
    pass


class ContactFrequency(ContactObject):
    """
    Contact frequency (atomic and residue) for a trajectory.

    The contact frequency is defined as fraction of the trajectory that a
    certain contact is made. This object calculates this quantity for all
    contacts with atoms in the `query` residue, with "contact" defined as
    being within a certain cutoff distance.

    Parameters
    ----------
    trajectory : mdtraj.Trajectory
        Trajectory (segment) to analyze
    query_residues : list of int
        Indices of the residues to be included as query. Default `None`
        means all atoms.
    cutoff : float
        Cutoff distance for contacts, in nanometers. Default 0.45.
    n_neighbors_ignored : int
        Number of neighboring residues (in the same chain) to ignore.
        Default 2.
    """
    def __init__(self, trajectory, query=None, haystack=None, cutoff=0.45,
                 n_neighbors_ignored=2, frames=None):
        self._trajectory = trajectory
        if frames is None:
            frames = range(len(trajectory))
        self.frames = frames
        self._n_frames = len(frames)
        super(ContactFrequency, self).__init__(trajectory.topology,
                                               query, haystack, cutoff,
                                               n_neighbors_ignored)
        self._build_contact_map()

    def _build_contact_map(self):
        # We actually build the contact map on a per-residue basis, although
        # we save it on a per-atom basis. This allows us ignore
        # n_nearest_neighbor residues.
        # TODO: this whole thing should be cleaned up and should replace
        # MDTraj's really slow old computer_contacts by using MDTraj's new
        # neighborlists (unless the MDTraj people do that first).
        topology = self.topology
        trajectory = self.trajectory
        cutoff = self.cutoff
        self._atom_contacts_count = collections.Counter([])
        self._residue_contacts_count = collections.Counter([])

        # cache things that can be calculated once based on the topology
        # (namely, which atom indices matter for each residue)
        residue_ignore_atom_idxs = self.residue_ignore_atom_idxs
        residue_query_atom_idxs = self.residue_query_atom_idxs
        for frame_num in self.frames:
            frame_contacts = self.contact_map(trajectory, frame_num,
                                              residue_query_atom_idxs,
                                              residue_ignore_atom_idxs)
            frame_atom_contacts = frame_contacts[0]
            frame_residue_contacts = frame_contacts[1]
            # self._atom_contacts_count += frame_atom_contacts
            self._atom_contacts_count.update(frame_atom_contacts)
            self._residue_contacts_count += frame_residue_contacts

    @property
    def trajectory(self):
        return self._trajectory

    @property
    def n_frames(self):
        return self._n_frames

    @property
    def atom_contacts(self):
        """Atoms pairs mapped to fraction of trajectory with that contact"""
        n_x = self.topology.n_atoms #len(self.haystack)
        n_y = self.topology.n_atoms #len(self.query)
        return ContactCount(collections.Counter({
            item[0]: float(item[1])/self.n_frames
            for item in self._atom_contacts_count.items()
        }), self.topology.atom, n_x, n_y)

    @property
    def residue_contacts(self):
        """Residue pairs mapped to fraction of trajectory with that contact"""
        n_x = self.topology.n_residues
        n_y = self.topology.n_residues
        return ContactCount(collections.Counter({
            item[0]: float(item[1])/self.n_frames
            for item in self._residue_contacts_count.items()
        }), self.topology.residue, n_x, n_y)


class ContactDifference(ContactObject):
    """
    Contact map comparison (atomic and residue).

    This can compare single frames or entire trajectories (or even mix the
    two!)
    """
    def __init__(self, positive, negative):
        self.positive = positive
        self.negative = negative
        # TODO: verify that the combination is compatible: same topol, etc
        super(ContactDifference, self).__init__(positive.topology,
                                                positive.query,
                                                positive.haystack,
                                                positive.cutoff,
                                                positive.n_neighbors_ignored)

    def __sub__(self, other):
        raise NotImplementedError

    def contact_map(self, *args, **kwargs):
        raise NotImplementedError

    @property
    def atom_contacts(self):
        n_x = self.topology.n_atoms
        n_y = self.topology.n_atoms
        diff = collections.Counter(self.positive.atom_contacts.counter)
        diff.subtract(self.negative.atom_contacts.counter)
        return ContactCount(diff, self.topology.atom, n_x, n_y)

    @property
    def residue_contacts(self):
        n_x = self.topology.n_residues
        n_y = self.topology.n_residues
        diff = collections.Counter(self.positive.residue_contacts.counter)
        diff.subtract(self.negative.residue_contacts.counter)
        return ContactCount(diff, self.topology.residue, n_x, n_y)


class ContactPlotter(object):

    def __init__(self, figsize=(6, 6), dpi=80, cmap='seismic', xmin=None,
                 xmax=None, ymin=None, ymax=None, vmin=-1, vmax=1,
                 marker_size=None, *args, **kwargs):
        self.figsize = figsize
        self.init_dpi = dpi
        self.cmap = cmap
        self.init_xmin = xmin
        self.init_xmax = xmax
        self.init_ymin = ymin
        self.init_ymax = ymax
        self.vmin = vmin
        self.vmax = vmax
        self.init_marker_size = marker_size
        self.args = args
        self.kwargs = kwargs

    def dok2xyz(self, dok_matrix):
        x = []
        y = []
        z = []
        for item in dok_matrix.items():
            try:
                x.append(item[0][0])
                y.append(item[0][1])
                z.append(item[1])
            except IndexError:
                raise TypeError('Expected a DOK matrix, got a ' +
                                type(dok_matrix))
        return x, y, z

    def set_plot_limits(self, x, y, z):
        xfigsize, yfigsize = self.figsize
        if self.init_xmin is None:
            self.xmin = min(x)
        else:
            self.xmin = self.init_xmin
        if self.init_xmax is None:
            self.xmax = max(x)
        else:
            self.xmax = self.init_xmax
        if self.init_ymin is None:
            self.ymin = min(y)
        else:
            self.ymin = self.init_ymin
        if self.init_ymax is None:
            self.ymax = max(y)
        else:
            self.ymax = self.init_ymax

        self.dpi = self.init_dpi
        if ((self.xmax-self.xmin)/self.dpi) > xfigsize:
            old_dpi = self.dpi
            self.dpi = int(np.ceil((self.xmax-self.xmin)/xfigsize))
            warnings.warn('Increased standard dpi from ' + str(old_dpi) +
                          ' to ' + str(self.dpi) + '.')
        if ((self.ymax-self.ymin)/self.dpi) > yfigsize:
            old_dpi = self.dpi
            self.dpi = int(np.ceil((self.ymax-self.ymin)/yfigsize))
            warnings.warn('Increased standard dpi from ' + str(old_dpi) +
                          ' to ' + str(self.dpi) + '.')

        if self.init_marker_size is None:
            xmarker_size = int(xfigsize*self.dpi/(self.xmax-self.xmin))
            ymarker_size = int(yfigsize*self.dpi/(self.ymax-self.ymin))
            self.marker_size = max([1, min([xmarker_size, ymarker_size])])
        else:
            self.marker_size = self.init_marker_size

    def plot(self, dok_matrix):
        x, y, z = self.dok2xyz(dok_matrix)
        self.set_plot_limits(x, y, z)
        fig = plt.figure(figsize=self.figsize, dpi=self.dpi)
        plt.xlim([self.xmin, self.xmax])
        plt.ylim([self.ymin, self.ymax])
        plt.scatter(x, y, c=z, cmap=self.cmap, vmin=self.vmin, vmax=self.vmax,
                    marker=',', s=self.marker_size, lw=0,
                    *self.args, **self.kwargs)
        return fig,
