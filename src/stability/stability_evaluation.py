import numpy as np

from scipy.signal import butter, lfilter
from scipy.spatial.distance import pdist, squareform
from tqdm import *

from geometry import find_channel_neighbors
from filtering import *

def clean_spike_train(spt):
    units = np.unique(spt[:, 1])
    spt[:, 1] += len(units)
    units += len(units)
    for i, u in enumerate(units):
        u_idx = spt[:, 1] == u
        spt[u_idx, 1] = i
    return spt


class RecordingBatchIterator(object):

    def __init__(self, rec_file, geom_file, sample_rate,
                 n_batches, batch_time_samples, n_chan, radius):
        """Sets up the object for reading from a binary file.
        
        Args:
            rec_file: str. Path to binary file that contains the
            raw recording file.
            geom_file: str. Path to text file containing the
            geometry file. The file should contain n_chan lines
            and each line should contain two numbers that
            are separated by ' '.
            sample_rate: int. Recording sample rate in Hz.
            n_batches: int. processes the recording in n_batches
            number of consecuitive segments that start from the
            beginning.
            batch_time_samples: int. Number of time samples per
            each batch to be used.
        """
        self.s_rate = sample_rate
        self.batch_time_samples = batch_time_samples
        self.n_batches = n_batches
        self.n_chan = n_chan
        self.radius = radius
        self.geometry = np.genfromtxt(geom_file, delimiter=' ')
        self.neighbs = find_channel_neighbors(
            self.geometry, self.radius)
        self.file = open(rec_file, 'r')

    def next_batch(self):
        """Gets the next temporal batch of recording."""
        ts = np.fromfile(
            self.file,
            count= self.n_chan * self.batch_time_samples,
            dtype=np.int16)
        ts = np.reshape(ts, [self.batch_time_samples, self.n_chan])
        ts = butterworth(ts, 300, 0.1, 3, self.s_rate)
        ts = ts/np.std(ts)
        ts = whitening(ts, self.neighbs, 40)
        return ts

    def reset_cursor(self):
        """Resets the cursor of the open file to the beginning."""
        self.file.seek(0)

    def close_iterator(self):
        self.file.close()

class MeanWaveCalculator(object):

    def __init__(self, batch_reader, spike_train):
        """Sets up the object for mean wave computation.

        args:
            spt: numpy.ndarray of shape [N, 2] where N is the total
            number of events. First column indicates the spike times
            in time sample and second is cluster identity of the
            spike times.
        """
        self.batch_reader = batch_reader
        self.spike_train = spike_train
        self.window = range(-10, 30)
        self.spike_train = clean_spike_train(
            self.spike_train)
        self.n_units = max(self.spike_train[:, 1] + 1)
        self.templates = np.zeros(
            [len(self.window), batch_reader.n_chan, self.n_units])
        print('Computing mean waveforms...')
        status = self.compute_templates(5)
        print('Spike time on boundary {} times.'.format(status))

    def compute_templates(self, n_batches):
        """Computes the templates from a given number of batches."""
        counts = np.zeros(self.n_units)
        boundary_violation = 0
        n_samples = self.batch_reader.batch_time_samples
        for i in tqdm(range(n_batches)):
            batch_idx = np.logical_and(
                self.spike_train[:, 0] > i * n_samples,
                self.spike_train[:, 0] < (i+1) * n_samples)
            spt = self.spike_train[batch_idx, :]
            spt[:, 0] -= n_samples * i
            ts = self.batch_reader.next_batch()
            for j in range(spt.shape[0]):
                try:
                    self.templates[:, :, spt[j, 1]] += ts[spt[j, 0] + self.window, :]
                    counts[spt[j, 1]] += 1
                except:
                    boundary_violation += 1
        for u in range(self.n_units):
            if counts[u]:
                self.templates[:, :, u] /= counts[u]
        return boundary_violation

    def close_reader(self):
        self.batch_reader.close()


class RecordingAugmentation(object):

    def __init__(self, mean_wave_calculator):
        """Sets up the object for stability metric computations.

        Args:
            mean_wave_calculator: MeanWaveCalculator object.
        """
        self.template_comp = mean_wave_calculator
        self.geometry = mean_wave_calculator.batch_reader.geometry
        self.n_chan = self.geometry.shape[0]
        self.template_calculator = mean_wave_calculator
        self.x_unit = 20.0
        self.construct_channel_map()
        self.compute_stat_summary()

    def construct_channel_map(self):
        """Constucts a map of coordinate to channel index."""
        self.geom_map = {}
        for i in range(self.n_chan):
            self.geom_map[(self.geometry[i, 0], self.geometry[i, 1])] = i

    def move_spatial_trace(self, template, dist, spatial_size=10, mode='amp'):
        """Moves the waveform spatially around the probe.

        template: numpy.ndarray of shape [T, C]
        spatial_size: int. How many channels comprise the
        spatial trace of the given template.
        mode: str. Main channels are detected using amplitude if
        'amp' and energy otherwise.
        """
        new_temp = np.zeros(template.shape)
        if mode == 'amp':
            location = np.argsort(
                np.max(np.abs(template), axis=0))[-spatial_size:]
        x_move = dist * self.x_unit
        # the vector of translation from original location to new one.
        trans = np.zeros([len(location), 2]).astype('int') - 1
        trans[:, 0] = location
        for i, l in enumerate(location):
            candidate = (self.geometry[l, 0] + x_move, self.geometry[l, 1])
            if candidate in self.geom_map:
                trans[i, 1] = self.geom_map[candidate]
            else:
                continue
        idx_origin = trans[trans[:, 1] >= 0, 0]
        idx_moved = trans[trans[:, 1] >=0, 1]
        new_temp[:, idx_moved] = template[:, idx_origin]
        return new_temp

    def compute_stat_summary(self):
        """Sets up statistic summary of given spike train.

        This function models the difference in time sample
        between consecutive firings of a particular unit
        as a log-normal distribution.

        Returns:
            np.ndarray of shape [U, 3] where U is
            the number of units in the spike train. The columns of
            the summary respectively correspond to mean, standard
            devation of the log-normal and the total count of spikes
            for units.
        """
        self.stat_summary = np.zeros(
            [self.template_comp.n_units, 3])
        spt = self.template_comp.spike_train
        for u in range(self.template_comp.n_units):
            # spike train of unit u
            spt_u = np.sort(spt[spt[:, 1] == u, 0])
            if len(spt > 2):
                # We estimate the difference between
                # consecutive firing times of the same unit
                u_firing_diff = spt_u[1:] - spt_u[:-1]
                # Getting rid of duplicates.
                # TODO: do this more sensibly.
                u_firing_diff[u_firing_diff == 0] = 1
                u_firing_diff = np.log(u_firing_diff)
                u_mean = np.mean(u_firing_diff)
                u_std = np.std(u_firing_diff)
                self.stat_summary[u, :] = u_mean, u_std, len(spt_u)
        return self.stat_summary

    def make_fake_spike_train(self, augment_rate = 0.25):
        """Augments the data and saves the result to binary.

        Args:
            augment_rate: float between 0 and 1. Augmented spikes
            per unit (percentage of total spikes per unit).
        """
        refractory_period = 60
        spt = self.template_comp.spike_train
        # We sample a new set of spike times per cluster.
        times = []
        cid = []
        for u in range(self.template_comp.n_units):
            spt_u = np.sort(spt[spt[:, 1] == u, 0])
            new_spike_count = int(
                self.stat_summary[u, 2] * augment_rate)
            diffs = np.exp(np.random.normal(
                self.stat_summary[u, 0],
                self.stat_summary[u, 1],
                new_spike_count)).astype('int')
            # Offsets for adding new spikes based on the
            # sampled differential times.
            offsets = np.sort(
                np.random.choice(spt_u, new_spike_count, replace=False))
            
            diffs[diffs < refractory_period] += refractory_period
            times += list(offsets + diffs)
            cid += [u] * new_spike_count
        return np.array([times, cid]).T

    def save_augment_recording(
        self, out_file_name, length, move_rate=0.2, scale=1e3):
        """Augments recording and saves it to file.

        Args:
            out_file_name: str. Name of output file where the
            augmented recording is writen to.
            length: int. length of augmented recording in batch
            size of the originial batch iterator object which is
            in the mean wave calculatro object.
            move_rate: float between 0 and 1. Percentage of units
            whose augmented spike wave form is spatially moved.

        Returns:
            numpy.ndarray. The new ground truth spike train.
        """
        reader = self.template_comp.batch_reader
        reader.reset_cursor()
        # Determine which clusters are spatially moved.
        orig_templates = self.template_comp.templates
        n_units = self.template_comp.n_units
        # list of unit numbers which we move spatially.
        moved_units = np.sort(
            np.random.choice(range(n_units),
                             int(move_rate * n_units),
                             replace=False))
        temp_shape = self.template_comp.templates.shape
        moved_templates = np.zeros(
            [temp_shape[0], temp_shape[1], len(moved_units)])
        # An array size of n_units where 0 indicates no movement
        # otherwise the index of the moved template in the move_templates
        # np.ndarray.
        moved = np.zeros(n_units)
        for i, u in enumerate(moved_units):
            moved[u] = i
            # Spatial distance is drawn from a poisson distribution.
            dist = np.sign(np.random.rand() - 0.5) * np.random.poisson(15)
            moved_templates[:, :, i] = self.move_spatial_trace(
                orig_templates[:, :, u], dist)
        # Create augmented spike train.
        aug_spt = self.make_fake_spike_train()
        reader = self.template_comp.batch_reader
        boundary_violation = 0
        n_samples = reader.batch_time_samples
        f = open(out_file_name, 'w')
        for i in tqdm(range(length)):
            batch_idx = np.logical_and(
                aug_spt[:, 0] > i * n_samples,
                aug_spt[:, 0] < (i+1) * n_samples)
            spt = aug_spt[batch_idx, :]
            spt[:, 0] -= n_samples * i
            ts = reader.next_batch()
            for j in range(spt.shape[0]):
                cid = spt[j, 1]
                try:
                    if moved[cid]:
                        ts[spt[j, 0] + self.window, :] += moved_templates[:, :, moved[cid]]
                    else:
                        ts[spt[j, 0] + self.window, :] += orig_templates[:, :, cid]
                except:
                    boundary_violation += 1
            ts *= scale
            ts = ts.astype('int16')
            ts.tofile(f)
        # Reassign spikes from moved clusters to new units.
        new_unit_id = self.template_comp.n_units
        for u in range(self.template_comp.n_units):
            if moved[u]:
                aug_spt[aug_spt[:, 1] == u, 1] = new_unit_id
                new_unit_id += 1
        f.close()
        return np.append(
            self.template_comp.spike_train, aug_spt, axis=0)
        
            
class SpikeSortingEvaluation(object):

    def __init__(self, spt_base, spt):
        """Sets up the evaluation object with two spike trains.

            Args:
            spt_base: numpy.ndarray of shape [N, 2]. base line spike
            trian. First column is spike times and second the cluster
            identities.
            spt: numpy.ndarray of shame [M, 2].
        """
        # clean the spike train before calling this function.
        spt_base = clean_spike_train(spt_base)
        spt = clean_spike_train(spt)
        self.n_units = np.max(spt_base[:, 1]) + 1
        self.n_clusters = np.max(spt[:, 1]) + 1
        self.spt_base = spt_base
        self.spt = spt
        # Spike counts per unit and cluster
        self.spike_count_base = self.count_spikes(spt_base)
        self.spike_count_cluster = self.count_spikes(spt)
        # Compute matching and accuracies.
        self.confusion_matrix = None
        self.compute_confusion_matrix()
        self.true_positive = np.zeros(self.n_units)
        self.false_positive = np.zeros(self.n_units)
        self.unit_cluster_map = np.zeros(self.n_units, dtype='int')
        self.compute_accuracies()
        
    def count_spikes(self, spt):
        """Counts spike events per cluster/units.

        Args:
            spt: numpy.ndarray of shape [N, 2]. Clean spike
            train where cluster ids are 0, ..., N-1.
        """
        n_cluster = np.max(spt[:, 1]) + 1
        counts = np.zeros(n_cluster)
        for u in range(n_cluster):
            counts[u] = np.sum(spt[:, 1] == u)
        return counts

    def compute_confusion_matrix(self):
        """Calculates the confusion matrix of two spike trains.

        The first spike train is the instances original spike train.
        The second one is given as an argument. 
        """
        confusion_matrix = np.zeros(
            [self.n_units, self.n_clusters])
        for unit in tqdm(range(self.n_units)):
            idx = self.spt_base[:, 1] == unit
            spike_times_base = self.spt_base[idx, 0]
            for cluster in range(self.n_clusters):
                idx = self.spt[:, 1] == cluster
                spike_times_cluster = self.spt[idx, 0]
                confusion_matrix[unit, cluster] = self.count_matches(
                    spike_times_base, spike_times_cluster)
        self.confusion_matrix = confusion_matrix

    def count_matches(self, array1, array2):
        """Finds the matches between two count process.

        Returns:
            int. Number of temporal collisions of spikes in
            array1 vs spikes in array2.
        """
        self.admissible_proximity = 60
        m, n = len(array1), len(array2)
        i, j = 0, 0
        count = 0
        while i < m and j < n:
            if abs(array1[i] - array2[j]) < self.admissible_proximity:
                i += 1
                j += 1
                count += 1
            elif array1[i] < array2[j]:
                i += 1
            else:
                j += 1
        return count

    def compute_accuracies(self):
        """Computes the TP/FP accuracies for the given spike trains."""
        self.unit_cluster_map = np.argmax(
            self.confusion_matrix, axis=0)
        recovered = np.max(self.confusion_matrix, axis=0)
        self.true_positive = recovered / self.spike_count_base
        match_count = self.spike_count_cluster[self.unit_cluster_map]
        self.false_positive = (match_count - recovered) / match_count
  