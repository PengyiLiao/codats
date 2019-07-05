"""
Models

Primarily we can't be using just sequential because of the grl_lambda needing
to be passed around. Also residual connections need to be a separate custom
layer.

Provides the model DomainAdaptationModel() and its components along with the
make_{task,domain}_loss() functions. Also, compute_accuracy() if desired.
"""
import numpy as np
import tensorflow as tf

from absl import flags
from tensorflow.python.keras import backend as K

from tcn import TemporalConvNet

FLAGS = flags.FLAGS

flags.DEFINE_float("dropout", 0.05, "Dropout probability")

flags.register_validator("dropout", lambda v: v != 1, message="dropout cannot be 1")


@tf.custom_gradient
def flip_gradient(x, grl_lambda=1.0):
    """ Forward pass identity, backward pass negate gradient and multiply by  """
    grl_lambda = tf.cast(grl_lambda, dtype=tf.float32)

    def grad(dy):
        return tf.negative(dy) * grl_lambda * tf.ones_like(x)

    return x, grad


class FlipGradient(tf.keras.layers.Layer):
    """
    Gradient reversal layer

    global_step = tf.Variable storing the current step
    schedule = a function taking the global_step and computing the grl_lambda,
        e.g. `lambda step: 1.0` or some more complex function.
    """
    def __init__(self, global_step, grl_schedule, **kwargs):
        super().__init__(**kwargs)
        self.global_step = global_step
        self.grl_schedule = grl_schedule

    def call(self, inputs, **kwargs):
        """ Calculate grl_lambda first based on the current global step (a
        variable) and then create the layer that does nothing except flip
        the gradients """
        grl_lambda = self.grl_schedule(self.global_step)
        return flip_gradient(inputs, grl_lambda)


def ConstantGrlSchedule(constant=1.0):
    """ Constant GRL schedule (always returns the same number) """
    def schedule(step):
        return constant
    return schedule


def DisableGrlSchedule():
    """ Setting grl_lambda=-0.1 removes any effect from it """
    def schedule(step):
        return -1.0
    return schedule


def DannGrlSchedule(num_steps):
    """ GRL schedule from DANN paper """
    num_steps = tf.cast(num_steps, tf.float32)

    def schedule(step):
        step = tf.cast(step, tf.float32)
        return 2/(1+tf.exp(-10*(step/(num_steps+1))))-1

    return schedule


class StopGradient(tf.keras.layers.Layer):
    """ Stop gradient layer """
    def call(self, inputs, **kwargs):
        return tf.stop_gradient(inputs)


def make_dense_bn_dropout(units, dropout):
    return tf.keras.Sequential([
        tf.keras.layers.Dense(units, use_bias=False),  # BN has a bias term
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.Activation("relu"),
        tf.keras.layers.Dropout(dropout),
    ])


def make_dense_ln_dropout(units, dropout):
    return tf.keras.Sequential([
        tf.keras.layers.Dense(units, use_bias=False),  # BN has a bias term
        tf.keras.layers.LayerNormalization(),
        tf.keras.layers.Activation("relu"),
        tf.keras.layers.Dropout(dropout),
    ])


class ResnetBlock(tf.keras.layers.Layer):
    """ Block consisting of other blocks but with residual connections """
    def __init__(self, units, dropout, layers, layer_norm=False, **kwargs):
        super().__init__(**kwargs)
        if layer_norm:
            self.blocks = [make_dense_ln_dropout(units, dropout) for _ in range(layers)]
        else:
            self.blocks = [make_dense_bn_dropout(units, dropout) for _ in range(layers)]
        self.add = tf.keras.layers.Add()

    def call(self, inputs, **kwargs):
        """ Like Sequential but with a residual connection """
        shortcut = inputs
        net = inputs

        for block in self.blocks:
            net = block(net, **kwargs)

        return self.add([shortcut, net], **kwargs)


class WangResnetBlock(tf.keras.layers.Layer):
    """
    ResNet block for the "ResNet" model by Wang et al. (2017)
    See make_resnet_model()
    """
    def __init__(self, n_feature_maps, shortcut_resize=True, **kwargs):
        super().__init__(**kwargs)
        self.blocks = [
            tf.keras.Sequential([
                tf.keras.layers.Conv1D(filters=n_feature_maps, kernel_size=8,
                    padding="same", use_bias=False),
                tf.keras.layers.BatchNormalization(),
                tf.keras.layers.Activation("relu"),
            ]),
            tf.keras.Sequential([
                tf.keras.layers.Conv1D(filters=n_feature_maps, kernel_size=5,
                    padding="same", use_bias=False),
                tf.keras.layers.BatchNormalization(),
                tf.keras.layers.Activation("relu"),
            ]),
            tf.keras.Sequential([
                tf.keras.layers.Conv1D(filters=n_feature_maps, kernel_size=3,
                    padding="same", use_bias=False),
                tf.keras.layers.BatchNormalization(),
            ]),
        ]
        if shortcut_resize:
            self.shortcut = tf.keras.Sequential([
                tf.keras.layers.Conv1D(filters=n_feature_maps, kernel_size=1,
                    padding="same", use_bias=False),
                tf.keras.layers.BatchNormalization(),
            ])
        else:
            self.shortcut = tf.keras.Sequential([
                tf.keras.layers.BatchNormalization(),
            ])
        self.add = tf.keras.layers.Add()
        self.act = tf.keras.layers.Activation("relu")

    def call(self, inputs, **kwargs):
        net = inputs

        for block in self.blocks:
            net = block(net, **kwargs)

        shortcut = self.shortcut(inputs, **kwargs)
        add = self.add([net, shortcut], **kwargs)

        return self.act(add, **kwargs)


class Conv1DTranspose(tf.keras.layers.Layer):
    """
    Conv2DTranspose modified for 1D tensors, for use with time series data
    See: https://stackoverflow.com/a/45788699

    Note: Conv2DTranspose requires 4D tensor (batch_size, x, y, channels), so
    we expand our data to (batch_size, time_steps, 1, num_features).
    """
    def __init__(self, filters, kernel_size, padding="same", strides=1,
            use_bias=True, **kwargs):
        super().__init__(**kwargs)
        self.conv2d = tf.keras.layers.Conv2DTranspose(filters=filters,
            kernel_size=(kernel_size, 1), padding=padding, strides=(strides, 1),
            use_bias=use_bias)

    def call(self, inputs, **kwargs):
        net = tf.expand_dims(inputs, 2)
        net = self.conv2d(net, **kwargs)
        net = tf.squeeze(net, axis=2)
        return net


def make_vrada_model(num_classes, global_step, grl_schedule):
    """
    Create model inspired by the VRADA paper model for time-series data

    Note: VRADA model had a VRNN though rather than just flattening data and
    didn't use residual connections.
    """
    fe_layers = 5
    task_layers = 1
    domain_layers = 2
    resnet_layers = 2
    units = 50
    dropout = FLAGS.dropout

    # General classifier used in both the task/domain classifiers
    def make_classifier(layers, num_outputs):
        layers = [make_dense_bn_dropout(units, dropout) for _ in range(layers-1)]
        last = [
            tf.keras.layers.Dense(num_outputs),
            tf.keras.layers.Activation("softmax"),
        ]
        return tf.keras.Sequential(layers + last)

    def make_binary_classifier(layers):
        layers = [make_dense_bn_dropout(units, dropout) for _ in range(layers-1)]
        last = [tf.keras.layers.Dense(1)]
        return tf.keras.Sequential(layers + last)

    feature_extractor = tf.keras.Sequential([
        tf.keras.layers.Flatten(),
        tf.keras.layers.BatchNormalization(momentum=0.999),
    ] + [  # First can't be residual since x isn't of size units
        make_dense_bn_dropout(units, dropout) for _ in range(resnet_layers)
    ] + [
        ResnetBlock(units, dropout, resnet_layers) for _ in range(fe_layers-1)
    ])
    task_classifier = tf.keras.Sequential([
        make_classifier(task_layers, num_classes),
    ])
    domain_classifier = tf.keras.Sequential([
        FlipGradient(global_step, grl_schedule),
        make_binary_classifier(domain_layers),
    ])
    return feature_extractor, task_classifier, domain_classifier


def make_timenet_model(num_classes, global_step, grl_schedule):
    """
    TimeNet https://arxiv.org/pdf/1706.08838.pdf
    So, basically 3-layer GRU with 60 units followed by the rest in my "flat"
    model above in make_vrada_model(). TimeNet doesn't seem to use dropout,
    though HealthNet in https://arxiv.org/pdf/1904.00655.pdf does.
    """
    fe_layers = 5
    task_layers = 1
    domain_layers = 2
    resnet_layers = 2
    units = 50
    dropout = FLAGS.dropout

    # General classifier used in both the task/domain classifiers
    def make_classifier(layers, num_outputs):
        layers = [make_dense_bn_dropout(units, dropout) for _ in range(layers-1)]
        last = [
            tf.keras.layers.Dense(num_outputs),
            tf.keras.layers.Activation("softmax"),
        ]
        return tf.keras.Sequential(layers + last)

    def make_binary_classifier(layers):
        layers = [make_dense_bn_dropout(units, dropout) for _ in range(layers-1)]
        last = [tf.keras.layers.Dense(1)]
        return tf.keras.Sequential(layers + last)

    feature_extractor = tf.keras.Sequential([
        # Normalize along features, shape is (batch_size, time_steps, features)
        # since for GRU the data probably needs to be normalized
        tf.keras.layers.BatchNormalization(momentum=0.999, axis=2),
        tf.keras.layers.GRU(60, return_sequences=True),
        tf.keras.layers.GRU(60, return_sequences=True),
        tf.keras.layers.GRU(60),
        tf.keras.layers.Flatten(),
    ] + [  # First can't be residual since x isn't of size units
        make_dense_bn_dropout(units, dropout) for _ in range(resnet_layers)
    ] + [
        ResnetBlock(units, dropout, resnet_layers) for _ in range(fe_layers-1)
    ])
    task_classifier = tf.keras.Sequential([
        make_classifier(task_layers, num_classes),
    ])
    domain_classifier = tf.keras.Sequential([
        FlipGradient(global_step, grl_schedule),
        make_binary_classifier(domain_layers),
    ])
    return feature_extractor, task_classifier, domain_classifier


def make_mlp_model(num_classes, global_step, grl_schedule):
    """
    MLP -- but split task/domain classifier at last dense layer, and additional
    dense layer for domain classifier

    From: https://arxiv.org/pdf/1611.06455.pdf
    Tested in: https://arxiv.org/pdf/1809.04356.pdf
    Code from: https://github.com/hfawaz/dl-4-tsc/blob/master/classifiers/mlp.py
    """
    feature_extractor = tf.keras.Sequential([
        # Normalize along features, shape is (batch_size, time_steps, features)
        tf.keras.layers.BatchNormalization(momentum=0.999, axis=2),
        tf.keras.layers.Flatten(),
        tf.keras.layers.Dropout(0.1),
        tf.keras.layers.Dense(500, activation="relu"),
        tf.keras.layers.Dropout(0.2),
        tf.keras.layers.Dense(500, activation="relu"),
        tf.keras.layers.Dropout(0.2),
    ])
    task_classifier = tf.keras.Sequential([
        tf.keras.layers.Dense(500, activation="relu"),
        tf.keras.layers.Dropout(0.3),
        tf.keras.layers.Dense(num_classes, activation="softmax"),
    ])
    domain_classifier = tf.keras.Sequential([
        FlipGradient(global_step, grl_schedule),

        tf.keras.layers.Dense(500, use_bias=False),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.Activation("relu"),
        tf.keras.layers.Dropout(0.3),

        tf.keras.layers.Dense(500, use_bias=False),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.Activation("relu"),
        tf.keras.layers.Dropout(0.3),

        tf.keras.layers.Dense(1),
    ])
    return feature_extractor, task_classifier, domain_classifier


def make_fcn_model(num_classes, global_step, grl_schedule):
    """
    FCN (fully CNN) -- but domain classifier has additional dense layers

    From: https://arxiv.org/pdf/1611.06455.pdf
    Tested in: https://arxiv.org/pdf/1809.04356.pdf
    Code from: https://github.com/hfawaz/dl-4-tsc/blob/master/classifiers/fcn.py
    """
    feature_extractor = tf.keras.Sequential([
        # Normalize along features, shape is (batch_size, time_steps, features)
        tf.keras.layers.BatchNormalization(momentum=0.999, axis=2),

        tf.keras.layers.Conv1D(filters=128, kernel_size=8, padding="same",
            use_bias=False),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.Activation("relu"),

        tf.keras.layers.Conv1D(filters=256, kernel_size=5, padding="same",
            use_bias=False),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.Activation("relu"),

        tf.keras.layers.Conv1D(filters=128, kernel_size=3, padding="same",
            use_bias=False),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.Activation("relu"),

        tf.keras.layers.GlobalAveragePooling1D(),
    ])
    task_classifier = tf.keras.Sequential([
        tf.keras.layers.Dense(num_classes, activation="softmax"),
    ])
    domain_classifier = tf.keras.Sequential([
        # Note: alternative is Dense(128, activation="tanh") like used by
        # https://arxiv.org/pdf/1902.09820.pdf They say dropout of 0.7 but
        # I'm not sure if that means 1-0.7 = 0.3 or 0.7 itself.
        FlipGradient(global_step, grl_schedule),

        tf.keras.layers.Dense(500, use_bias=False),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.Activation("relu"),
        tf.keras.layers.Dropout(0.3),

        tf.keras.layers.Dense(500, use_bias=False),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.Activation("relu"),
        tf.keras.layers.Dropout(0.3),

        tf.keras.layers.Dense(1),
    ])
    return feature_extractor, task_classifier, domain_classifier


def make_resnet_model(num_classes, global_step, grl_schedule):
    """
    ResNet -- but domain classifier has additional dense layers

    From: https://arxiv.org/pdf/1611.06455.pdf
    Tested in: https://arxiv.org/pdf/1809.04356.pdf
    Code from: https://github.com/hfawaz/dl-4-tsc/blob/master/classifiers/resnet.py
    """
    feature_extractor = tf.keras.Sequential([
        # Normalize along features, shape is (batch_size, time_steps, features)
        tf.keras.layers.BatchNormalization(momentum=0.999, axis=2),
        WangResnetBlock(64),
        WangResnetBlock(128),
        WangResnetBlock(128, shortcut_resize=False),
        tf.keras.layers.GlobalAveragePooling1D(),
    ])
    task_classifier = tf.keras.Sequential([
        tf.keras.layers.Dense(num_classes, activation="softmax"),
    ])
    domain_classifier = tf.keras.Sequential([
        FlipGradient(global_step, grl_schedule),

        tf.keras.layers.Dense(500, use_bias=False),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.Activation("relu"),
        tf.keras.layers.Dropout(0.3),

        tf.keras.layers.Dense(500, use_bias=False),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.Activation("relu"),
        tf.keras.layers.Dropout(0.3),

        tf.keras.layers.Dense(1),
    ])
    return feature_extractor, task_classifier, domain_classifier


def make_dann_mnist_model(num_classes, global_step, grl_schedule):
    """ Figure 4(a) MNIST architecture -- Ganin et al. DANN JMLR 2016 paper """
    feature_extractor = tf.keras.Sequential([
        tf.keras.layers.Conv2D(32, (5, 5), (1, 1), "valid", activation="relu"),
        tf.keras.layers.MaxPool2D((2, 2), (2, 2), "valid"),
        tf.keras.layers.Conv2D(48, (5, 5), (1, 1), "valid", activation="relu"),
        tf.keras.layers.MaxPool2D((2, 2), (2, 2), "valid"),
        tf.keras.layers.Flatten(),
    ])
    task_classifier = tf.keras.Sequential([
        tf.keras.layers.Dense(100, "relu"),
        tf.keras.layers.Dense(100, "relu"),
        tf.keras.layers.Dense(num_classes, "softmax"),
    ])
    domain_classifier = tf.keras.Sequential([
        FlipGradient(global_step, grl_schedule),
        tf.keras.layers.Dense(100, "relu"),
        tf.keras.layers.Dense(1),
    ])
    return feature_extractor, task_classifier, domain_classifier


def make_dann_svhn_model(num_classes, global_step, grl_schedule):
    """ Figure 4(b) SVHN architecture -- Ganin et al. DANN JMLR 2016 paper """
    dropout = FLAGS.dropout

    feature_extractor = tf.keras.Sequential([
        tf.keras.layers.Conv2D(64, (5, 5), (1, 1), "same"),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.ReLU(),

        tf.keras.layers.MaxPool2D((3, 3), (2, 2), "same"),
        tf.keras.layers.Dropout(dropout),

        tf.keras.layers.Conv2D(64, (5, 5), (1, 1), "same"),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.ReLU(),

        tf.keras.layers.MaxPool2D((3, 3), (2, 2), "same"),
        tf.keras.layers.Dropout(dropout),

        tf.keras.layers.Conv2D(128, (5, 5), (1, 1), "same"),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.ReLU(),

        tf.keras.layers.Flatten(),
    ])
    task_classifier = tf.keras.Sequential([
        tf.keras.layers.Dense(3072),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.ReLU(),
        tf.keras.layers.Dropout(dropout),

        tf.keras.layers.Dense(2048),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.ReLU(),
        tf.keras.layers.Dropout(dropout),

        tf.keras.layers.Dense(num_classes, "softmax"),
    ])
    domain_classifier = tf.keras.Sequential([
        FlipGradient(global_step, grl_schedule),
        tf.keras.layers.Dense(1024),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.ReLU(),
        tf.keras.layers.Dropout(dropout),

        tf.keras.layers.Dense(1024),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.ReLU(),
        tf.keras.layers.Dropout(dropout),

        tf.keras.layers.Dense(1),
    ])
    return feature_extractor, task_classifier, domain_classifier


def make_dann_gtsrb_model(num_classes, global_step, grl_schedule):
    """ Figure 4(c) SVHN architecture -- Ganin et al. DANN JMLR 2016 paper """
    feature_extractor = tf.keras.Sequential([
        tf.keras.layers.Conv2D(96, (5, 5), (1, 1), "valid", activation="relu"),
        tf.keras.layers.MaxPool2D((2, 2), (2, 2), "valid"),
        tf.keras.layers.Conv2D(144, (3, 3), (1, 1), "valid", activation="relu"),
        tf.keras.layers.MaxPool2D((2, 2), (2, 2), "valid"),
        tf.keras.layers.Conv2D(256, (5, 5), (1, 1), "valid", activation="relu"),
        tf.keras.layers.MaxPool2D((2, 2), (2, 2), "valid"),
        tf.keras.layers.Flatten(),
    ])
    task_classifier = tf.keras.Sequential([
        tf.keras.layers.Dense(512, "relu"),
        tf.keras.layers.Dense(num_classes, "softmax"),
    ])
    domain_classifier = tf.keras.Sequential([
        FlipGradient(global_step, grl_schedule),
        tf.keras.layers.Dense(1024, "relu"),
        tf.keras.layers.Dense(1024, "relu"),
        tf.keras.layers.Dense(1),
    ])
    return feature_extractor, task_classifier, domain_classifier


def make_vada_model(num_classes, global_step, grl_schedule,
        small=False):
    """ Table 6 Small CNN -- Shu et al. VADA / DIRT-T ICLR 2018 paper

    Note: they used small for digits, traffic signs, and WiFi and large for
    CIFAR-10 and STL-10."""
    leak_alpha = 0.1

    def conv_blocks(depth):
        return [
            tf.keras.layers.Conv2D(depth, (3, 3), (1, 1), "same"),
            tf.keras.layers.BatchNormalization(),
            tf.keras.layers.LeakyReLU(leak_alpha),

            tf.keras.layers.Conv2D(depth, (3, 3), (1, 1), "same"),
            tf.keras.layers.BatchNormalization(),
            tf.keras.layers.LeakyReLU(leak_alpha),

            tf.keras.layers.Conv2D(depth, (3, 3), (1, 1), "same"),
            tf.keras.layers.BatchNormalization(),
            tf.keras.layers.LeakyReLU(leak_alpha),
        ]

    def pool_blocks():
        return [
            tf.keras.layers.MaxPool2D((2, 2), (2, 2), "same"),
            tf.keras.layers.Dropout(0.5),
            tf.keras.layers.GaussianNoise(1),
        ]

    feature_extractor = tf.keras.Sequential(
        conv_blocks(64 if small else 96)
        + pool_blocks()
        + conv_blocks(64 if small else 192)
        + pool_blocks())
    task_classifier = tf.keras.Sequential(
        conv_blocks(64 if small else 192)
        + [
            tf.keras.layers.GlobalAvgPool2D(),
            tf.keras.layers.Flatten(),
            tf.keras.layers.Dense(num_classes, "softmax"),
        ])
    domain_classifier = tf.keras.Sequential([
        FlipGradient(global_step, grl_schedule),
        tf.keras.layers.Flatten(),

        tf.keras.layers.Dense(100),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.ReLU(),

        tf.keras.layers.Dense(1),
    ])
    return feature_extractor, task_classifier, domain_classifier


def make_resnet50_model(num_classes, global_step, grl_schedule):
    """ ResNet50 pre-trained on ImageNet -- for use with Office-31 datasets
    Input should be 224x224x3 """
    feature_extractor = tf.keras.applications.ResNet50(
        include_top=False, pooling="avg")
    task_classifier = tf.keras.Sequential([
        tf.keras.layers.Flatten(),
        tf.keras.layers.Dense(num_classes, "softmax"),
    ])
    domain_classifier = tf.keras.Sequential([
        FlipGradient(global_step, grl_schedule),
        tf.keras.layers.Flatten(),
        tf.keras.layers.Dense(1),
    ])
    return feature_extractor, task_classifier, domain_classifier


class DomainAdaptationModel(tf.keras.Model):
    """
    Domain adaptation model -- task and domain classifier outputs, depends on
    command line --model=X argument

    Usage:
        model = DomainAdaptationModel(num_classes, "flat",
            global_step, num_steps)

        with tf.GradientTape() as tape:
            task_y_pred, domain_y_pred = model(x, training=True)
            ...
    """
    def __init__(self, num_classes, model_name, global_step,
            num_steps, use_grl=False, **kwargs):
        super().__init__(**kwargs)
        if use_grl:
            grl_schedule = DannGrlSchedule(num_steps)
            #grl_schedule = ConstantGrlSchedule(0.01)  # Possibly for VADA
        else:
            grl_schedule = DisableGrlSchedule()

        args = (num_classes, global_step, grl_schedule)

        if model_name == "flat":
            fe, task, domain = make_vrada_model(*args)
        elif model_name == "timenet":
            fe, task, domain = make_timenet_model(*args)
        elif model_name == "mlp":
            fe, task, domain = make_mlp_model(*args)
        elif model_name == "fcn":
            fe, task, domain = make_fcn_model(*args)
        elif model_name == "resnet":
            fe, task, domain = make_resnet_model(*args)
        elif model_name == "dann_mnist":
            fe, task, domain = make_dann_mnist_model(*args)
        elif model_name == "dann_svhn":
            fe, task, domain = make_dann_svhn_model(*args)
        elif model_name == "dann_gtsrb":
            fe, task, domain = make_dann_gtsrb_model(*args)
        elif model_name == "vada_small":
            fe, task, domain = make_vada_model(*args, small=True)
        elif model_name == "vada_large":
            fe, task, domain = make_vada_model(*args, small=False)
        elif model_name == "resnet50":
            fe, task, domain = make_resnet50_model(*args)
        else:
            raise NotImplementedError("Model name: "+str(model_name))

        self.feature_extractor = fe
        self.task_classifier = task
        self.domain_classifier = domain

        # Target classifier (if used) will be the same as the task classifier
        # but will be trained on pseudo-labeled data. Then, call
        # model(..., target=True) to use this classifier rather than the task
        # classifier.
        self.target_classifier = tf.keras.models.clone_model(task)

    @property
    def trainable_variables_task(self):
        return self.feature_extractor.trainable_variables \
            + self.task_classifier.trainable_variables

    @property
    def trainable_variables_task_domain(self):
        return self.feature_extractor.trainable_variables \
            + self.task_classifier.trainable_variables \
            + self.domain_classifier.trainable_variables

    @property
    def trainable_variables_target(self):
        return self.feature_extractor.trainable_variables \
            + self.target_classifier.trainable_variables

    def call(self, inputs, target=False, training=None, **kwargs):
        # Manually set the learning phase since we probably aren't using .fit()
        if training is True:
            tf.keras.backend.set_learning_phase(1)
        elif training is False:
            tf.keras.backend.set_learning_phase(0)

        fe = self.feature_extractor(inputs, **kwargs)
        domain = self.domain_classifier(fe, **kwargs)

        # If desired, use the target classifier rather than the task classifier
        if target:
            task = self.target_classifier(fe, **kwargs)
        else:
            task = self.task_classifier(fe, **kwargs)

        return task, domain


class CycleGAN(tf.keras.Model):
    """ Domain mapping model -- based on CycleGAN, but for time-series data
    instead of image data, also partially based on VRADA model """
    def __init__(self, source_x_shape, target_x_shape, **kwargs):
        super().__init__(**kwargs)
        assert target_x_shape == source_x_shape, \
            "Right now only support homogenous adaptation"

        self.source_to_target = self.make_generator(target_x_shape)
        self.target_to_source = self.make_generator(source_x_shape)
        self.source_discriminator = self.make_discriminator()
        self.target_discriminator = self.make_discriminator()

        # Pass source/target data through these layers first, but only set
        # training=True when feeding through real data. Note: axis=2 to be the
        # feature axis. Defaults to 1 I think, which would be the time axis.
        #
        # source_pre -- run on source-like data, before source_to_target model
        self.source_pre = tf.keras.Sequential([
            tf.keras.layers.BatchNormalization(momentum=0.999, axis=2),
        ])
        # target_pre -- run on target-like data, before target_to_source model
        self.target_pre = tf.keras.Sequential([
            tf.keras.layers.BatchNormalization(momentum=0.999, axis=2),
        ])

    def make_generator(self, output_dims):
        assert len(output_dims) == 2, \
            "output_dims should be length 2, (time_steps, num_features)"
        num_features = output_dims[1]

        return tf.keras.Sequential([
            tf.keras.layers.Conv1D(filters=8, kernel_size=8, padding="same",
                use_bias=False),
            tf.keras.layers.BatchNormalization(),
            tf.keras.layers.Activation("relu"),

            tf.keras.layers.Conv1D(filters=16, kernel_size=5, padding="same",
                strides=2, use_bias=False),
            tf.keras.layers.BatchNormalization(),
            tf.keras.layers.Activation("relu"),

            tf.keras.layers.Conv1D(filters=32, kernel_size=3, padding="same",
                strides=2, use_bias=False),
            tf.keras.layers.BatchNormalization(),
            tf.keras.layers.Activation("relu"),

            WangResnetBlock(32, shortcut_resize=False),
            WangResnetBlock(32, shortcut_resize=False),

            Conv1DTranspose(filters=16, kernel_size=3, padding="same",
                strides=2, use_bias=False),
            tf.keras.layers.BatchNormalization(),
            tf.keras.layers.Activation("relu"),

            Conv1DTranspose(filters=8, kernel_size=5, padding="same",
                strides=2, use_bias=False),
            tf.keras.layers.BatchNormalization(),
            tf.keras.layers.Activation("relu"),

            tf.keras.layers.Conv1D(filters=num_features, kernel_size=8,
                padding="same", use_bias=False),
            tf.keras.layers.BatchNormalization(),
            tf.keras.layers.Activation("relu"),

            # For bias mainly, so output can have any range of values
            # TODO replace Flatten() with a per-feature flattening when dealing
            # with multivariate data?
            tf.keras.layers.Flatten(),
            tf.keras.layers.Dense(np.prod(output_dims), use_bias=True),
            tf.keras.layers.Reshape(output_dims),
        ])

    def make_discriminator(self):
        # FCN classifier, but slightly smaller
        # TODO use layernorm not batchnorm when FLAGS.cyclegan_loss == "wgan-gp"
        # TODO maybe try random crop for discriminator
        return tf.keras.Sequential([
            tf.keras.layers.Conv1D(filters=64, kernel_size=8, padding="same",
                use_bias=False),
            tf.keras.layers.BatchNormalization(),
            tf.keras.layers.Activation("relu"),

            tf.keras.layers.Conv1D(filters=128, kernel_size=5, padding="same",
                use_bias=False),
            tf.keras.layers.BatchNormalization(),
            tf.keras.layers.Activation("relu"),

            tf.keras.layers.Conv1D(filters=64, kernel_size=3, padding="same",
                use_bias=False),
            tf.keras.layers.BatchNormalization(),
            tf.keras.layers.Activation("relu"),

            tf.keras.layers.GlobalAveragePooling1D(),
            tf.keras.layers.Dense(1),
        ])

    @property
    def trainable_variables_generators(self):
        return self.source_to_target.trainable_variables \
            + self.target_to_source.trainable_variables \
            + self.source_pre.trainable_variables \
            + self.target_pre.trainable_variables

    @property
    def trainable_variables_discriminators(self):
        return self.source_discriminator.trainable_variables \
            + self.target_discriminator.trainable_variables

    def set_learning_phase(self, training=None):
        """ Manually set the learning phase since we probably aren't using .fit() """
        if training is True:
            tf.keras.backend.set_learning_phase(1)
        elif training is False:
            tf.keras.backend.set_learning_phase(0)

    def call(self, inputs, dest, training=None, **kwargs):
        """
        Example for training:
            gen_AtoB, gen_AtoBtoA, disc_Areal, disc_Bfake = model(x_a, "target", training=True)
            gen_BtoA, gen_BtoAtoB, disc_Breal, disc_Afake = model(x_b, "source", training=True)

        Example for testing:
            gen_AtoB, gen_AtoBtoA, _, _ = model(map_x_a, "target", training=False)
            gen_BtoA, gen_BtoAtoB, _, _ = model(map_x_b, "source", training=False)
        """
        self.set_learning_phase(training)

        if dest == "target":  # A to B
            x_a = inputs

            # BN for normalization, train batch norm only on original data
            x_a_norm = self.source_pre(x_a, training=training)

            # Map to target
            gen_AtoB = self.source_to_target(x_a_norm, training=training, **kwargs)

            # BN for normalization, but never train on the fake data
            x_b_fake_norm = self.target_pre(gen_AtoB, training=False)

            # Map back to source
            gen_AtoBtoA = self.target_to_source(x_b_fake_norm, training=training, **kwargs)

            # Discriminator outputs, both run on the normalized data
            disc_Areal = self.source_discriminator(x_a_norm, training=training, **kwargs)
            disc_Bfake = self.target_discriminator(x_b_fake_norm, training=training, **kwargs)

            return gen_AtoB, gen_AtoBtoA, disc_Areal, disc_Bfake

        elif dest == "source":  # B to A
            x_b = inputs

            # BN for normalization, train batch norm only on original data
            x_b_norm = self.target_pre(x_b, training=training)

            # Map to source
            gen_BtoA = self.target_to_source(x_b_norm, training=training, **kwargs)

            # BN for normalization, but never train on the fake data
            x_a_fake_norm = self.source_pre(gen_BtoA, training=False)

            # Map back to target
            gen_BtoAtoB = self.source_to_target(x_a_fake_norm, training=training, **kwargs)

            # Discriminator outputs, both run on the normalized data
            disc_Breal = self.target_discriminator(x_b_norm, training=training, **kwargs)
            disc_Afake = self.source_discriminator(x_a_fake_norm, training=training, **kwargs)

            return gen_BtoA, gen_BtoAtoB, disc_Breal, disc_Afake

        else:
            raise NotImplementedError("dest can only be either source or target")

    def map_to_target(self, x):
        """ Map source data to target, but make sure we don't update BN stats """
        self.set_learning_phase(False)
        return self.source_to_target(self.source_pre(x, training=False), training=False)

    def map_to_source(self, x):
        """ Map target data to source, but make sure we don't update BN stats """
        self.set_learning_phase(False)
        return self.target_to_source(self.target_pre(x, training=False), training=False)


def make_task_loss(adapt):
    """
    The same as CategoricalCrossentropy() but only on half the batch if doing
    adaptation and in the training phase
    """
    cce = tf.keras.losses.CategoricalCrossentropy()

    def task_loss(y_true, y_pred, training=None):
        """
        Compute loss on the outputs of the task classifier

        Note: domain classifier can use normal tf.keras.losses.CategoricalCrossentropy
        but for the task loss when doing adaptation we need to ignore the second half
        of the batch since this is unsupervised
        """
        if training is None:
            training = K.learning_phase()

        # If doing domain adaptation, then we'll need to ignore the second half of the
        # batch for task classification during training since we don't know the labels
        # of the target data
        if adapt and training:
            batch_size = tf.shape(y_pred)[0]
            y_pred = tf.slice(y_pred, [0, 0], [batch_size // 2, -1])
            y_true = tf.slice(y_true, [0, 0], [batch_size // 2, -1])

        return cce(y_true, y_pred)

    return task_loss


def make_weighted_loss():
    """ The same as CategoricalCrossentropy() but weighted """
    cce = tf.keras.losses.CategoricalCrossentropy()

    def task_loss(y_true, y_pred, weights, training=None):
        """
        Compute loss on the outputs of a classifier weighted by the specified
        weights
        """
        return cce(y_true, y_pred, sample_weight=weights)

    return task_loss


def make_domain_loss(use_domain_loss):
    """
    Just CategoricalCrossentropy() but for consistency with make_task_loss()
    """
    if use_domain_loss:
        # from_logits=True means we didn't pass the Dense(1) layer through any
        # activation function like sigmoid. If we need the "probability" later,
        # then we'll have to manually pass it through a sigmoid function.
        cce = tf.keras.losses.BinaryCrossentropy(from_logits=True)

        def domain_loss(y_true, y_pred):
            """ Compute loss on the outputs of the domain classifier """
            return cce(y_true, y_pred)
    else:
        def domain_loss(y_true, y_pred):
            """ Domain loss only used during adaptation """
            return 0

    return domain_loss


def make_mapping_loss():
    """
    Just CategoricalCrossentropy() but for consistency with make_task_loss()
    """
    # from_logits=True means we didn't pass the Dense(1) layer through any
    # activation function like sigmoid. If we need the "probability" later,
    # then we'll have to manually pass it through a sigmoid function.
    cce = tf.keras.losses.BinaryCrossentropy(from_logits=True)

    def mapping_loss(y_true, y_pred):
        """ Compute loss on the outputs of the discriminators """
        return cce(y_true, y_pred)

    return mapping_loss


def compute_accuracy(y_true, y_pred):
    return tf.reduce_mean(input_tensor=tf.cast(
        tf.equal(tf.argmax(y_true, axis=-1), tf.argmax(y_pred, axis=-1)),
        tf.float32))


# List of names
models = [
    "flat",
    "timenet",
    "mlp",
    "fcn",
    "resnet",
    "dann_mnist",
    "dann_svhn",
    "dann_gtsrb",
    "vada_small",
    "vada_large",
    "resnet50",
]


# Get names
def names():
    """
    Returns list of all the available models for use in DomainAdaptationModel()
    """
    return models
