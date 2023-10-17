import warnings

import matplotlib.pyplot as plt
import pandas as pd
import numpy as np
import sklearn.base
import sklearn.preprocessing as pre
import torch.utils.data
from torch.utils.data import Dataset
from enum import Enum
from dataset.utils import Sampler

DEFAULT_ROOT = r"/home/fuen/DeepLearningProjects/FaultdiagnosisDataset/" \
               r"IEEE PHM 2008涡轮风扇发动机退化仿真数据集CMAPSSData.zip/data/"

DEFAULT_SENSORS = ["s_2", "s_3", "s_4", "s_7", "s_8", "s_9", "s_11", "s_12", "s_13",
                   "s_14", "s_15", "s_17", "s_20", "s_21"]


class Subset(Enum):
    FD001 = "FD001"
    FD002 = "FD002"
    FD003 = "FD003"
    FD004 = "FD004"


class Cmapss(Dataset):
    """
    The C-MAPSS dataset used for DataLoader.

    Notes
    -----
    This class is supported to use dataset.utils.Sampler to customizing your own sampling method. If you do that, the
    sampling method when DataLoader calling __getitem__(index) will be changed. The Sampler could be indicated when
    you initialize Cmapss(... , sampler =...) or use method set_sampler(sampler = ...).
    """

    def __init__(self, data: np.ndarray, ids: np.ndarray, labels: np.ndarray, sampler: Sampler = None):
        """
        The dataset class used for DataLoader.

        :param data: The CMAPSS dataset samples.
        :param ids: The engine id of every sample.
        :param labels: The RUL label of every sample.
        """
        self.data = data
        self.ids = ids
        self.labels = labels
        assert self.data.shape[0] == self.ids.shape[0] == self.labels.shape[0]
        self.__sampler = sampler if sampler is not None else None

    def __getitem__(self, item):
        if self.__sampler is not None:
            return self.__sampler.sample(item)
        else:
            return self.data[item], self.labels[item:item+1]

    def __len__(self):
        return self.data.shape[0]

    def set_sampler(self, sampler: Sampler):
        self.__sampler = sampler

    def clear_sampler(self):
        self.__sampler = None

    def get_data_by_engine_id(self, engine_id):
        pass


class CmapssNegativeSampler(Sampler):
    """
    A Sampler used to construct a negative-positive pair to train a Contrastive Neural Network
    """

    def __init__(self, dataset: Cmapss, engine_num=1, interval_num=4):
        """
        
        :param dataset:
        :param engine_num:
        :param interval_num:
        """
        super(CmapssNegativeSampler, self).__init__(dataset)
        dataset.set_sampler(self)
        self.ids = dataset.ids
        self.data = dataset.data
        self.labels = dataset.labels
        self.interval_nums = interval_num
        self.engine_num = engine_num

    def sample(self, index: int):
        engine_id = self.ids[index]
        engine_ids = np.random.choice(a=np.unique(self.ids),
                                      size=self.engine_num,
                                      replace=False)
        if engine_id not in engine_ids:
            engine_ids[0] = engine_id  # 保证index所在的引擎被采样
        neg_samples = [0] * (self.interval_nums * self.engine_num)  # +1 to eliminate the target sample itself.
        neg_labels = [0] * (self.interval_nums * self.engine_num)
        neg_ids = [0] * (self.interval_nums * self.engine_num)
        j = 1  # 负样本数组索引，负样本的数组从1开始存入负样本采样结果，因为0位置需要放入正样本
        # start sampling
        for engine in engine_ids:
            sample_indexes = np.argwhere(self.ids == engine)
            gap = sample_indexes.shape[0] // self.interval_nums
            for i in range(self.interval_nums):
                random_range_start = sample_indexes[0][0] + i * gap
                # 在最后一次循环内，保证采样边界到达同设备样本的最后一个下标，防止出现漏采
                random_range_end = random_range_start + gap \
                    if i != self.interval_nums - 1 else sample_indexes[-1][0] + 1
                if random_range_start <= index < random_range_end and engine == engine_id:
                    continue
                sample_index = np.random.choice(range(random_range_start, random_range_end), 1, replace=True)
                neg_samples[j] = self.data[sample_index[0]]
                neg_labels[j] = self.labels[sample_index[0]]  # n:n+1的方式保持label拥有最后一个维度
                neg_ids[j] = self.ids[sample_index[0]]
                j += 1
        # 最终数组的首位放入正样本
        neg_samples[0] = self.data[index]
        neg_labels[0] = self.labels[index]  # n:n+1的方式来保持label拥有最后一个维度
        neg_ids[0] = engine_id  # 用于测试，查看是否所有负样本与正样本来自同一个引擎
        return np.stack(neg_samples), np.array(neg_labels)


class CmapssRandomNegtiveSampler(Sampler):
    def __init__(self, dataset: Cmapss, neg_num=10, sample_thresh=0.2):
        super(CmapssRandomNegtiveSampler, self).__init__(dataset)
        dataset.set_sampler(self)
        self.neg_num = neg_num
        self.labels = dataset.labels
        self.data = dataset.data
        self.thresh = sample_thresh

    def sample(self, index: int):
        indexes = np.squeeze(np.argwhere(np.abs(self.labels-self.labels[index]) > self.thresh))
        indexes = np.random.choice(indexes, size=self.neg_num+1, replace=False)
        indexes[0] = index
        return self.data[indexes], self.labels[indexes]



def generate_rul(df: pd.DataFrame, y_test: pd.DataFrame = None, normalize=False, threshold=0) -> pd.DataFrame:
    """
    Generating RUL labels for original DataFrame.

    :param df: The CMAPSS DataFrame generated by get_data() methods.
    :param y_test: The DataFrame from RUL_FD00N.txt file. If not None, this method will process the df as training data,
                   else this method will process the df as test data.
    :param normalize: Weather normalizing the RUL label to [0, 1].
    :param threshold: Weather drop the RUL which bigger than the threshold. This argument will be processed earlier than
                      normalize argument. Thus, if normalize = True, the dropped RUL will be 1.
    :return: A DataFrame contains RUL column with name "rul" and the maximum life cycle column with name "max_cycles".
    """
    grouped = df.groupby(by="unit_nr")
    RUL_max = grouped["time_cycles"].max()
    if y_test is not None:
        y_test.index = RUL_max.index
        RUL_max = RUL_max + y_test[y_test.columns[0]]
    result = pd.merge(df, RUL_max.to_frame(name="max_cycles"), on="unit_nr")
    result["rul"] = result["max_cycles"] - result["time_cycles"]
    if threshold > 0:
        result.loc[result["rul"] > threshold, "rul"] = threshold
        result.loc[result["max_cycles"] > threshold, "max_cycles"] = threshold + 1
    if normalize:
        result["rul"] = (result["rul"] + 1) / result["max_cycles"]
    # result.drop("max_cycles", axis=1)
    return result


def generate_window_sample(df: pd.DataFrame, window_size, slide_step, sensors):
    """
    Transform the RULed DataFrame to window samples.

    :param df: The RULed DataFrame.
    :param window_size: The sample length.
    :param slide_step: Sampling step size.
    :param sensors: The sensors' data will be returned. If None, will return all the sensors' data.
    :return: [ndarray with window samples; ndarray with engine id for every window samples; ndarray with
              RUL labels for every window samples]
    """
    engine_grouped = df.groupby(by="unit_nr")
    result = [0] * len(engine_grouped)  # engine sensor data
    engine_ids = [0] * len(engine_grouped)  # engine id
    labels = [0] * len(engine_grouped)  # rul labels
    i = 0
    for _, engine in list(engine_grouped):
        data = engine[sensors].values  # shape = (n, f)
        if data.shape[0] < window_size:
            warnings.warn("The engine id {} with total length {} is shorter than window_size {}. "
                          "Hence, these samples were dropped!".format(_, data.shape[0], window_size))
            continue
        s = [0] * ((data.shape[0] - window_size) // slide_step)  # temporal sensor data
        e = [0] * ((data.shape[0] - window_size) // slide_step)  # temporal engine data. To correspond with each sample.
        rul = [0] * ((data.shape[0] - window_size) // slide_step)  # temporal rul data. To correspond with each sample.
        for j in range(len(s)):
            s[j] = data[j:j + window_size]
            e[j] = engine["unit_nr"].iloc[0]
            rul[j] = engine["rul"].iloc[
                j + window_size]  # The label is set to the last time stamp of the sample window.
        result[i] = s
        engine_ids[i] = e
        labels[i] = rul
        i += 1
    return np.concatenate(result[:i], dtype=np.float64), \
           np.concatenate(engine_ids[:i], dtype=np.float64), \
           np.concatenate(labels[:i], dtype=np.float64)


def get_data(path: str, subset: Subset, window_size: int, slide_step: int = 1, sensors: list = None,
             scaler: sklearn.base.TransformerMixin = pre.MinMaxScaler((-1, 1)), rul_threshold=0, label_norm=False,
             val_ratio=0.2):
    """
    Return the training data, test data and validation data of C-MAPSS dataset.

    :param path: The root path of the C-MAPSS dataset. The cmapss.DEAFULT_ROOT is the default root path in server.
    :param subset: A enum indicated the subset. Should be the element of follows: [FD001, FD002, FD003, FD004]
    :param window_size: The sample length.
    :param slide_step: The sampling gap length, default 1.
    :param sensors: The sensor data will be returned. It should be from [s_1 ~ s_21]. If None, selecting all the
                    sensor data.
    :param scaler: Used for normalizing the train and test data. It should be a sklearn scaler.
    :param rul_threshold: The rul threshold is applied to a piecewise linear RUL label function. If 0, will applied
                          non-piecewise linear RUL label function.
    :param label_norm: Weather normalizing the RUL label to [0, 1].
    :param val_ratio: The ratio of validation dataset.

    :return: train data set class (torch.utils.data.Dataset),
             test data set class (torch.utils.data.Dataset),
             val data set class (torch.utils.data.Dataset),
             Scaler (maybe) used to inverse transform the train data.

    Notes
    -----
    This method could only process the original C-MAPSS data, which is formatted as:
    RUL_FD00X.txt/train_FD00X.txt/test_FD00X.txt
    """
    # files
    train_file = 'train_' + subset.value + '.txt'
    test_file = 'test_' + subset.value + '.txt'
    # columns
    index_names = ['unit_nr', 'time_cycles']
    setting_names = ['setting_1', 'setting_2', 'setting_3']
    sensor_names = ['s_{}'.format(i + 1) for i in range(0, 21)]
    col_names = index_names + setting_names + sensor_names
    # data readout
    train = pd.read_csv((path + train_file), sep=r'\s+', header=None,
                        names=col_names)
    test = pd.read_csv((path + test_file), sep=r'\s+', header=None,
                       names=col_names)
    y_test = pd.read_csv((path + 'RUL_' + subset.value + '.txt'), sep=r'\s+', header=None,
                         names=['RUL'])
    # generate rul label
    train = generate_rul(train, threshold=rul_threshold, normalize=label_norm)
    test = generate_rul(test, y_test, threshold=rul_threshold, normalize=label_norm)
    # split the val dataset from train set
    train, val = split_val_set(train, val_ratio)
    # normalization use train set (the normalization factors are all come from train set)
    assert isinstance(scaler, (pre.StandardScaler, pre.MinMaxScaler, pre.RobustScaler, pre.MaxAbsScaler))
    scaler.fit(train[sensors])
    train[sensors] = scaler.transform(train[sensors])
    test[sensors] = scaler.transform(test[sensors])
    val[sensors] = scaler.transform(val[sensors])

    if sensors is None or sensors == []:
        sensors = train.columns
    [train_data, train_ids, train_label] = generate_window_sample(train, window_size, slide_step, sensors)
    [val_data, val_ids, val_label] = generate_window_sample(val, window_size, slide_step, sensors)
    [test_data, test_ids, test_label] = generate_window_sample(test, window_size, slide_step, sensors)
    train_data = Cmapss(train_data, train_ids, train_label)
    test_data = Cmapss(test_data, test_ids, test_label)
    val_data = Cmapss(val_data, val_ids, val_label)
    return train_data, test_data, val_data, scaler


def split_val_set(train_set: pd.DataFrame, val_size=0.2):
    """
    This method is used to split the train_set to a train data and validation data. And normalize the validation data
    use the normalizing factors which are computed from train data.

    :param train_set: The data will be split.
    :param val_size: The validation data set ratio for train_set (Default 0.2).
    :return: train_data, val_data
    """
    grouped = train_set.groupby(by="unit_nr")
    train_set_result = []
    val_set_result = []
    val_index = np.random.choice(range(1, len(grouped) + 1), int(len(grouped) * val_size), replace=False)
    for i in range(1, len(grouped) + 1):
        data = train_set[train_set["unit_nr"] == i]
        if i in val_index:
            val_set_result.append(data)
        else:
            train_set_result.append(data)
    return pd.concat(train_set_result), pd.concat(val_set_result)


def plot_sensor_data(path: str, engine_id: int, subset: Subset, sensors: list = None):
    import matplotlib.pyplot as plt
    plt.switch_backend("agg")
    plt.figure(figsize=(10, 10), dpi=600)
    train_file = 'train_' + subset.value + '.txt'
    index_names = ['unit_nr', 'time_cycles']
    setting_names = ['setting_1', 'setting_2', 'setting_3']
    sensor_names = ['s_{}'.format(i + 1) for i in range(0, 21)]
    col_names = index_names + setting_names + sensor_names
    data = pd.read_csv((path + train_file), sep=r'\s+', header=None, names=col_names)
    if sensors is None:
        sensors = sensor_names
    l = len(sensors)
    i = 0
    for sensor in sensors:
        plt.subplot(7, 3, i + 1)
        plt.plot(data[data["unit_nr"] == engine_id][sensor])
        plt.title(sensor)
        i += 1
    plt.tight_layout()
    plt.savefig(r"/home/fuen/DeepLearningProjects/FaultDiagnosis/result.png", dpi=600)


if __name__ == '__main__':
    train1, test1, val1, scaler = get_data(DEFAULT_ROOT,
                                           Subset.FD001,
                                           window_size=32,
                                           slide_step=1,
                                           sensors=DEFAULT_SENSORS,
                                           rul_threshold=0,
                                           label_norm=True,
                                           scaler=pre.MinMaxScaler(),
                                           val_ratio=0.1)
    # sampler = CmapssNegativeSampler(train1, 10, 2)
    sampler = CmapssRandomNegtiveSampler(train1, 20)
    loader = torch.utils.data.DataLoader(train1, 32, True)
    for _, (x, y) in enumerate(loader):
        print(x.shape)
        print(y.shape)
        break
