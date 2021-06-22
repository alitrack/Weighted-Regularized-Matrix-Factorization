# Copyright 2018 The Cornac Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================

import os
from tqdm.auto import trange
from .base_recommender import Recommender
import cornac
from WRMF.wrmf_utils import *
from utils.common.constants import (
    DEFAULT_USER_COL,
    DEFAULT_ITEM_COL,
    DEFAULT_RATING_COL,
    DEFAULT_PREDICTION_COL,
)


def prepare_cornac_data(data):
    return cornac.data.Dataset.from_uir(
        data[[DEFAULT_USER_COL, DEFAULT_ITEM_COL, DEFAULT_RATING_COL]].itertuples(index=False)
    )


def train_cornac(model, data):
    train_data = prepare_cornac_data(data)
    model.fit(train_data)
    return model


class WRMF(Recommender):
    """Weighted Matrix Factorization.
    original source code - "https://github.com/PreferredAI/cornac/blob/master/cornac/models/wmf/recom_wmf.py"

    Parameters
    ----------
    name: string, default: 'WMF'
        The name of the recommender model.

    weight_strategy: string, default: 'uniform_pos'
        Weighting strategy - 'uniform_pos', 'uniform_neg', 'user_oriented', 'item-oriented', 'item_popularity'

    alpha: scalar, default: 1
        Hyper-parameter that controls the strength of weights

    c_0: scalar, default: 1
        Hyper-parameter of item-popularity strategy that determines the overall weight of unobserved instances.

    k: int, optional, default: 200
        The dimension of the latent factors.

    max_iter: int, optional, default: 100
        Maximum number of iterations or the number of epochs for SGD.

    learning_rate: float, optional, default: 0.001
        The learning rate for AdamOptimizer.

    lambda_u: float, optional, default: 0.01
        The regularization parameter for users.

    lambda_v: float, optional, default: 0.01
        The regularization parameter for items.

    a: float, optional, default: 1
        The confidence of observed ratings.

    b: float, optional, default: 0.01
        The confidence of unseen ratings.

    batch_size: int, optional, default: 128
        The batch size for SGD.

    trainable: boolean, optional, default: True
        When False, the model is not trained and Cornac assumes that the model already
        pre-trained (U and V are not None).

    init_params: dictionary, optional, default: None
        List of initial parameters, e.g., init_params = {'U':U, 'V':V}

        U: ndarray, shape (n_users,k)
            The user latent factors, optional initialization via init_params.

        V: ndarray, shape (n_items,k)
            The item latent factors, optional initialization via init_params.

    seed: int, optional, default: None
        Random seed for weight initialization.

    References
    ----------
    * Hu, Y., Koren, Y., & Volinsky, C. (2008, December). Collaborative filtering for implicit feedback datasets. \
    In 2008 Eighth IEEE International Conference on Data Mining (pp. 263-272).

    * Pan, R., Zhou, Y., Cao, B., Liu, N. N., Lukose, R., Scholz, M., & Yang, Q. (2008, December). \
    One-class collaborative filtering. In 2008 Eighth IEEE International Conference on Data Mining (pp. 502-511).

    """

    def __init__(
            self,
            data,
            name="WMF",
            weight_strategy="uniform_pos",
            alpha=1,
            c_0=1,
            k=200,
            lambda_u=0.01,
            lambda_v=0.01,
            learning_rate=0.001,
            batch_size=128,
            max_iter=100,
            trainable=True,
            verbose=True,
            init_params=None,
            seed=None,
    ):
        super().__init__(name=name, trainable=trainable, verbose=verbose)
        self.k = k
        self.lambda_u = lambda_u
        self.lambda_v = lambda_v
        self.strategy = weight_strategy
        self.data = data
        self.alpha = alpha
        self.c_0 = c_0

        if self.strategy == "user_oriented":
            self.weights = weight_user_oriented(self.data, self.alpha)
        elif self.strategy == "item_oriented":
            self.weights = weight_item_oriented(self.data, self.alpha)
        elif self.strategy == "item_popularity":
            self.weights = weight_item_popularity(self.data, self.alpha, self.c_0)
        elif self.strategy == "uniform_pos" or self.strategy == "uniform_neg":
            self.weights = np.ones(shape=data[DEFAULT_ITEM_COL].nunique()) * alpha
        else:
            print('wrong strategy')

        print('maximum of weights={}, minimum={}'.format(self.weights.max(), self.weights.min()))
        self.learning_rate = learning_rate
        self.name = name
        self.init_params = init_params
        self.max_iter = max_iter
        self.batch_size = batch_size
        self.verbose = verbose
        self.seed = seed

        # Init params if provided
        self.init_params = {} if init_params is None else init_params
        self.U = self.init_params.get("U", None)
        self.V = self.init_params.get("V", None)

    def _init(self):
        rng = get_rng(self.seed)
        n_users, n_items = self.train_set.num_users, self.train_set.num_items

        if self.U is None:
            self.U = xavier_uniform((n_users, self.k), rng)
        if self.V is None:
            self.V = xavier_uniform((n_items, self.k), rng)

    def fit(self, train_set, val_set=None):
        """Fit the model to observations.

        Parameters
        ----------
        train_set: :obj:`cornac.data.Dataset`, required
            User-Item preference data as well as additional modalities.

        val_set: :obj:`cornac.data.Dataset`, optional, default: None
            User-Item preference data for model selection purposes (e.g., early stopping).

        Returns
        -------
        self : object
        """
        Recommender.fit(self, train_set, val_set)

        self._init()

        if self.trainable:
            self._fit_cf()

        return self

    def _fit_cf(self, ):
        import tensorflow as tf
        from .wrmf_model import Model

        np.random.seed(self.seed)
        os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
        tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.ERROR)

        R = self.train_set.csc_matrix  # csc for efficient slicing over items
        n_users, n_items, = self.train_set.num_users, self.train_set.num_items

        # Build model
        graph = tf.Graph()
        with graph.as_default():
            tf.set_random_seed(self.seed)
            model = Model(
                n_users=n_users,
                n_items=n_items,
                k=self.k,
                lambda_u=self.lambda_u,
                lambda_v=self.lambda_v,
                lr=self.learning_rate,
                U=self.U,
                V=self.V,
            )

        # Training model
        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True
        with tf.Session(config=config, graph=graph) as sess:
            sess.run(tf.global_variables_initializer())

            loop = trange(self.max_iter, disable=not self.verbose)
            for _ in loop:

                sum_loss = 0
                count = 0
                for i, batch_ids in enumerate(
                        self.train_set.item_iter(self.batch_size, shuffle=True)
                ):
                    batch_R = R[:, batch_ids]

                    if self.strategy == "uniform_pos":
                        batch_C = np.ones(batch_R.shape)
                        batch_C[batch_R.nonzero()] = self.alpha
                    elif self.strategy == "uniform_neg":
                        batch_C = np.zeros(batch_R.shape) + self.alpha
                        batch_C[batch_R.nonzero()] = 1
                    else:
                        if self.strategy == "user_oriented":
                            weight_vec = self.weights.reshape(batch_R.shape[0], -1)
                        else:
                            weight_vec = self.weights[batch_ids].reshape(-1, len(batch_ids))

                        batch_C = np.zeros(batch_R.shape) + weight_vec
                        batch_C[batch_R.nonzero()] = 1

                    feed_dict = {
                        model.ratings: batch_R.A,
                        model.C: batch_C,
                        model.item_ids: batch_ids,
                    }
                    _, _loss = sess.run(
                        [model.opt, model.loss], feed_dict
                    )  # train U, V

                    sum_loss += _loss
                    count += len(batch_ids)
                    if i % 10 == 0:
                        loop.set_postfix(loss=(sum_loss / count))

            self.U, self.V = sess.run([model.U, model.V])

        tf.reset_default_graph()

        if self.verbose:
            print("Learning completed!")

    def score(self, user_idx, item_idx=None):
        """Predict the scores/ratings of a user for an item.

        Parameters
        ----------
        user_idx: int, required
            The index of the user for whom to perform score prediction.

        item_idx: int, optional, default: None
            The index of the item for which to perform score prediction.
            If None, scores for all known items will be returned.

        Returns
        -------
        res : A scalar or a Numpy array
            Relative scores that the user gives to the item or to all known items
        """
        if item_idx is None:
            if self.train_set.is_unk_user(user_idx):
                raise ScoreException(
                    "Can't make score prediction for (user_id=%d)" % user_idx
                )

            known_item_scores = self.V.dot(self.U[user_idx, :])
            return known_item_scores
        else:
            if self.train_set.is_unk_user(user_idx) or self.train_set.is_unk_item(
                    item_idx
            ):
                raise ScoreException(
                    "Can't make score prediction for (user_id=%d, item_id=%d)"
                    % (user_idx, item_idx)
                )
            user_pred = self.V[item_idx, :].dot(self.U[user_idx, :])
            return user_pred



