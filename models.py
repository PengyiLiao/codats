"""
Models
"""
import tensorflow as tf

from absl import flags
from vrnn import VRNN

FLAGS = flags.FLAGS

models = {}


def register_model(name):
    """ Add model to the list of models, e.g. add @register_model("name")
    before a class definition """
    assert name not in models, "duplicate model named " + name

    def decorator(cls):
        models[name] = cls
        return cls

    return decorator


def get_model(name, *args, **kwargs):
    """ Based on the given name, call the correct model """
    assert name in models.keys(), \
        "Unknown model name " + name
    return models[name](*args, **kwargs)


def list_models():
    """ Returns list of all the available models """
    return list(models.keys())


@tf.custom_gradient
def flip_gradient(x, grl_lambda):
    """ Forward pass identity, backward pass negate gradient and multiply by  """
    grl_lambda = tf.cast(grl_lambda, dtype=tf.float32)

    def grad(dy):
        # the 0 is for grl_lambda, which doesn't have a gradient
        return tf.negative(dy) * grl_lambda * tf.ones_like(x), 0

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


class ModelBase(tf.keras.Model):
    """ Base model class (inheriting from Keras' Model class) """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @property
    def trainable_variables_fe(self):
        return self.feature_extractor.trainable_variables

    @property
    def trainable_variables_task(self):
        return self.trainable_variables_fe \
            + self.task_classifier.trainable_variables

    @property
    def trainable_variables_domain(self):
        return self.domain_classifier.trainable_variables

    @property
    def trainable_variables_task_domain(self):
        return self.trainable_variables_fe \
            + self.task_classifier.trainable_variables \
            + self.trainable_variables_domain

    def set_learning_phase(self, training):
        # Manually set the learning phase since we probably aren't using .fit()
        # but layers like batch norm and dropout still need to know if
        # training/testing
        if training is True:
            tf.keras.backend.set_learning_phase(1)
        elif training is False:
            tf.keras.backend.set_learning_phase(0)

    # Allow easily overriding each part of the call() function, without having
    # to override call() in its entirety
    def call_feature_extractor(self, inputs, **kwargs):
        return self.feature_extractor(inputs, **kwargs)

    def call_task_classifier(self, fe, **kwargs):
        return self.task_classifier(fe, **kwargs)

    def call_domain_classifier(self, fe, task, **kwargs):
        return self.domain_classifier(fe, **kwargs)

    def call(self, inputs, training=None, **kwargs):
        self.set_learning_phase(training)
        fe = self.call_feature_extractor(inputs, **kwargs)
        task = self.call_task_classifier(fe, **kwargs)
        domain = self.call_domain_classifier(fe, task, **kwargs)
        return task, domain, fe


@register_model("fcn")
def make_model_fcn(num_classes, num_domains):
    """
    FCN (fully CNN) -- but domain classifier has additional dense layers

    From: https://arxiv.org/pdf/1611.06455.pdf
    Tested in: https://arxiv.org/pdf/1809.04356.pdf
    Code from: https://github.com/hfawaz/dl-4-tsc/blob/master/classifiers/fcn.py
    """
    feature_extractor = tf.keras.Sequential([
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
        tf.keras.layers.Dense(num_classes),
    ])
    domain_classifier = tf.keras.Sequential([
        # Note: alternative is Dense(128, activation="tanh") like used by
        # https://arxiv.org/pdf/1902.09820.pdf They say dropout of 0.7 but
        # I'm not sure if that means 1-0.7 = 0.3 or 0.7 itself.
        tf.keras.layers.Dense(500, use_bias=False),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.Activation("relu"),
        tf.keras.layers.Dropout(0.3),

        tf.keras.layers.Dense(500, use_bias=False),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.Activation("relu"),
        tf.keras.layers.Dropout(0.3),

        tf.keras.layers.Dense(num_domains),
    ])
    return feature_extractor, task_classifier, domain_classifier


class InceptionModule(tf.keras.layers.Layer):
    """ See make_model_inceptiontime() """
    def __init__(self, num_filters=32, activation="relu", **kwargs):
        super().__init__(**kwargs)
        self.num_filters = num_filters

        # Step 1
        self.bottleneck = self._conv1d(num_filters, kernel_size=1)
        self.maxpool = tf.keras.layers.MaxPool1D(pool_size=3, strides=1,
            padding="same")

        # Step 2
        #
        # Note: if kernel_size=40 in the original code, and
        # kernel_size_s = [self.kernel_size // (2 ** i) for i in range(3)]
        # then we get 40, 20, 10 (note order doesn't matter since we concatenate
        # them).
        self.z1 = self._conv1d(num_filters, kernel_size=10)
        self.z2 = self._conv1d(num_filters, kernel_size=20)
        self.z3 = self._conv1d(num_filters, kernel_size=40)
        self.z4 = self._conv1d(num_filters, kernel_size=1)

        # Step 3 -- concatenate along feature dimension (axis=2 or axis=-1)
        self.concat = tf.keras.layers.Concatenate(axis=-1)
        self.bn = tf.keras.layers.BatchNormalization()
        self.act = tf.keras.layers.Activation(activation)

    def get_config(self):
        config = super().get_config()
        config.update({
            "num_filters": self.num_filters,
            "activation": self.activation,
        })
        return config

    def _conv1d(self, filters, kernel_size):
        # Note: the blog post has some differences (presumably not matching the
        # paper's code then) leaves of padding="same" (implying padding="valid"
        # instead) and activation="relu" rather than activation="linear" in the
        # paper's code (or here activation=None, the default).
        #
        # Or, maybe this is TF vs. Keras default differences.
        return tf.keras.layers.Conv1D(filters=filters, kernel_size=kernel_size,
            padding="same", use_bias=False)

    def call(self, inputs, **kwargs):
        # Step 1
        Z_bottleneck = self.bottleneck(inputs, **kwargs)
        Z_maxpool = self.maxpool(inputs, **kwargs)

        # Step 2
        Z1 = self.z1(Z_bottleneck, **kwargs)
        Z2 = self.z2(Z_bottleneck, **kwargs)
        Z3 = self.z3(Z_bottleneck, **kwargs)
        Z4 = self.z4(Z_maxpool, **kwargs)

        # Step 3
        Z = self.concat([Z1, Z2, Z3, Z4])
        Z = self.bn(Z, **kwargs)

        return self.act(Z)


class InceptionShortcut(tf.keras.layers.Layer):
    """ Shortcut for InceptionBlock -- required separate for a separate build()
    since we don't know the right output dimension till running the network.

    See make_model_inceptiontime() """
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def build(self, input_shapes):
        Z_residual_shape, Z_inception_shape = input_shapes
        _, _, output_filters = Z_inception_shape

        self.shortcut_conv1d = tf.keras.layers.Conv1D(filters=output_filters,
            kernel_size=1, padding="same", use_bias=False)
        self.shortcut_bn = tf.keras.layers.BatchNormalization()
        self.shortcut_add = tf.keras.layers.Add()

    def call(self, inputs, **kwargs):
        Z_residual, Z_inception = inputs

        # Create shortcut connection
        Z_shortcut = self.shortcut_conv1d(Z_residual)
        Z_shortcut = self.shortcut_bn(Z_shortcut)

        # Add shortcut to Inception
        return self.shortcut_add([Z_shortcut, Z_inception])


class InceptionBlock(tf.keras.layers.Layer):
    """ Block consisting of 3 InceptionModules with shortcut at the end
    See make_model_inceptiontime() """
    def __init__(self, num_modules=3, activation="relu", **kwargs):
        super().__init__(**kwargs)
        self.num_modules = num_modules
        self.activation = activation
        self.modules = [InceptionModule() for _ in range(num_modules)]
        self.skip = InceptionShortcut()
        self.act = tf.keras.layers.Activation(activation)

    def get_config(self):
        config = super().get_config()
        config.update({
            "num_modules": self.num_modules,
            "activation": self.activation,
        })
        return config

    def call(self, inputs, **kwargs):
        Z = inputs
        Z_residual = inputs

        for i in range(self.num_modules):
            Z = self.modules[i](Z, **kwargs)

        Z = self.skip([Z_residual, Z], **kwargs)

        return self.act(Z)


class InceptionFeatureExtractor(tf.keras.layers.Layer):
    """ The entire InceptionTime feature extractor (just doesn't have last
    dense layer, i.e. stops at GAP). This isn't really needed but to ease
    the ensemble of multiple of these we create a "Layer" that defines all
    of this.

    Note: their code has num_modules=6, and every third has a skip connection.
    Thus, that's the same as 2 blocks.

    See make_model_inceptiontime() """
    def __init__(self, num_blocks=2, **kwargs):
        super().__init__(**kwargs)
        self.num_blocks = num_blocks
        self.seq = tf.keras.Sequential([
            InceptionBlock() for _ in range(num_blocks)
        ] + [
            tf.keras.layers.GlobalAveragePooling1D(),
        ])

    def get_config(self):
        """ Required to save __init__ args when cloning
        See: https://www.tensorflow.org/guide/keras/custom_layers_and_models#you_can_optionally_enable_serialization_on_your_layers
        """
        config = super().get_config()
        config.update({'num_blocks': self.num_blocks})
        return config

    def call(self, inputs, **kwargs):
        return self.seq(inputs, **kwargs)


@register_model("inceptiontime")
def make_model_inceptiontime(num_classes, num_domains):
    """
    InceptionTime -- but domain classifier has additional dense layers

    Paper: https://arxiv.org/pdf/1909.04939.pdf
    Keras code: https://towardsdatascience.com/deep-learning-for-time-series-classification-inceptiontime-245703f422db
    Paper's code: https://github.com/hfawaz/InceptionTime
    """
    feature_extractor = tf.keras.Sequential([
        InceptionFeatureExtractor(), # TODO ensemble of 5 of these??? 5 classifiers?
    ])
    # Copied from FCN -- note that InceptionTime is not designed for domain
    # adaptation, just for time series classification.
    task_classifier = tf.keras.Sequential([
        tf.keras.layers.Dense(num_classes),
    ])
    domain_classifier = tf.keras.Sequential([
        # Note: alternative is Dense(128, activation="tanh") like used by
        # https://arxiv.org/pdf/1902.09820.pdf They say dropout of 0.7 but
        # I'm not sure if that means 1-0.7 = 0.3 or 0.7 itself.
        tf.keras.layers.Dense(500, use_bias=False),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.Activation("relu"),
        tf.keras.layers.Dropout(0.3),

        tf.keras.layers.Dense(500, use_bias=False),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.Activation("relu"),
        tf.keras.layers.Dropout(0.3),

        tf.keras.layers.Dense(num_domains),
    ])
    return feature_extractor, task_classifier, domain_classifier


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


@register_model("mlp")
def make_mlp_model(num_classes, num_domains):
    """
    MLP -- but split task/domain classifier at last dense layer, and additional
    dense layer for domain classifier
    From: https://arxiv.org/pdf/1611.06455.pdf
    Tested in: https://arxiv.org/pdf/1809.04356.pdf
    Code from: https://github.com/hfawaz/dl-4-tsc/blob/master/classifiers/mlp.py
    """
    feature_extractor = tf.keras.Sequential([
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
        tf.keras.layers.Dense(num_classes),
    ])
    domain_classifier = tf.keras.Sequential([
        tf.keras.layers.Dense(500, use_bias=False),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.Activation("relu"),
        tf.keras.layers.Dropout(0.3),

        tf.keras.layers.Dense(500, use_bias=False),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.Activation("relu"),
        tf.keras.layers.Dropout(0.3),

        tf.keras.layers.Dense(num_domains),
    ])
    return feature_extractor, task_classifier, domain_classifier


class ReflectSamePadding(tf.keras.layers.Layer):
    """
    Output the same way that "same" padding would, but instead of zero padding
    do reflection padding.
    """
    def __init__(self, kernel_size, strides=1, **kwargs):
        super().__init__(**kwargs)
        self.kernel_size = kernel_size
        self.strides = strides

    def call(self, inputs, **kwargs):
        time_steps = inputs.shape[1]
        _, pad_before, pad_after = self.calc_padding(time_steps,
            self.kernel_size, self.strides, "same")
        # Note: for some reason works better when swapping before/after so that
        # for odd paddings, we have the extra padding at the left rather than
        # the right
        return tf.pad(inputs, [[0, 0], [pad_after, pad_before], [0, 0]], "reflect")

    def calc_padding(self, input_size, filter_size, stride, pad_type):
        """
        See code (used to be in the API guide but since has vanished):
        https://github.com/tensorflow/tensorflow/blob/master/tensorflow/core/framework/common_shape_fns.cc#L45
        Note: copied from my tflite code
        https://github.com/floft/vision-landing/blob/master/tflite_opencl.py
        """
        assert pad_type == "valid" or pad_type == "same", \
            "Only SAME and VALID padding types are implemented"

        if pad_type == "valid":
            output_size = int((input_size - filter_size + stride) / stride)
            pad_before = 0
            pad_after = 0
        elif pad_type == "same":
            output_size = int((input_size + stride - 1) / stride)
            pad_needed = max(0, (output_size - 1)*stride + filter_size - input_size)
            pad_before = pad_needed // 2
            pad_after = pad_needed - pad_before

        assert output_size >= 0, "output_size must be non-negative after padding"
        return output_size, pad_before, pad_after


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
    def __init__(self, n_feature_maps, shortcut_resize=True,
            kernel_sizes=[8, 5, 3], reflect_padding=False,
            normalization=tf.keras.layers.BatchNormalization,
            activation="relu", **kwargs):
        super().__init__(**kwargs)
        self.blocks = []

        for kernel_size in kernel_sizes:
            if reflect_padding:
                self.blocks.append(tf.keras.Sequential([
                    ReflectSamePadding(kernel_size),
                    tf.keras.layers.Conv1D(filters=n_feature_maps,
                        kernel_size=kernel_size,
                        padding="valid", use_bias=False),
                    normalization(),
                    tf.keras.layers.Activation(activation),
                ]))
            else:
                self.blocks.append(tf.keras.Sequential([
                    tf.keras.layers.Conv1D(filters=n_feature_maps,
                        kernel_size=kernel_size,
                        padding="same", use_bias=False),
                    normalization(),
                    tf.keras.layers.Activation(activation),
                ]))

        if shortcut_resize:
            self.shortcut = tf.keras.Sequential([
                tf.keras.layers.Conv1D(filters=n_feature_maps, kernel_size=1,
                    padding="same", use_bias=False),
                normalization(),
            ])
        else:
            self.shortcut = tf.keras.Sequential([
                normalization(),
            ])
        self.add = tf.keras.layers.Add()
        self.act = tf.keras.layers.Activation(activation)

    def call(self, inputs, **kwargs):
        net = inputs

        for block in self.blocks:
            net = block(net, **kwargs)

        shortcut = self.shortcut(inputs, **kwargs)
        add = self.add([net, shortcut], **kwargs)

        return self.act(add, **kwargs)


@register_model("resnet")
def make_resnet_model(num_classes, num_domains):
    """
    ResNet -- but domain classifier has additional dense layers
    From: https://arxiv.org/pdf/1611.06455.pdf
    Tested in: https://arxiv.org/pdf/1809.04356.pdf
    Code from: https://github.com/hfawaz/dl-4-tsc/blob/master/classifiers/resnet.py
    """
    feature_extractor = tf.keras.Sequential([
        WangResnetBlock(64),
        WangResnetBlock(128),
        WangResnetBlock(128, shortcut_resize=False),
        tf.keras.layers.GlobalAveragePooling1D(),
    ])
    task_classifier = tf.keras.Sequential([
        tf.keras.layers.Dense(num_classes),
    ])
    domain_classifier = tf.keras.Sequential([
        tf.keras.layers.Dense(500, use_bias=False),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.Activation("relu"),
        tf.keras.layers.Dropout(0.3),

        tf.keras.layers.Dense(500, use_bias=False),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.Activation("relu"),
        tf.keras.layers.Dropout(0.3),

        tf.keras.layers.Dense(num_domains),
    ])
    return feature_extractor, task_classifier, domain_classifier


@register_model("timenet")
def make_timenet_model(num_classes, num_domains):
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
        last = [tf.keras.layers.Dense(num_outputs)]
        return tf.keras.Sequential(layers + last)

    feature_extractor = tf.keras.Sequential([
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
        make_classifier(domain_layers, num_domains),
    ])
    return feature_extractor, task_classifier, domain_classifier


@register_model("images_dann_mnist")
def make_dann_mnist_model(num_classes, num_domains):
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
        tf.keras.layers.Dense(num_classes),
    ])
    domain_classifier = tf.keras.Sequential([
        tf.keras.layers.Dense(100, "relu"),
        tf.keras.layers.Dense(num_domains),
    ])
    return feature_extractor, task_classifier, domain_classifier


@register_model("images_dann_svhn")
def make_dann_svhn_model(num_classes, num_domains):
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

        tf.keras.layers.Dense(num_classes),
    ])
    domain_classifier = tf.keras.Sequential([
        tf.keras.layers.Dense(1024),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.ReLU(),
        tf.keras.layers.Dropout(dropout),

        tf.keras.layers.Dense(1024),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.ReLU(),
        tf.keras.layers.Dropout(dropout),

        tf.keras.layers.Dense(num_domains),
    ])
    return feature_extractor, task_classifier, domain_classifier


@register_model("images_dann_gtsrb")
def make_dann_gtsrb_model(num_classes, num_domains):
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
        tf.keras.layers.Dense(num_classes),
    ])
    domain_classifier = tf.keras.Sequential([
        tf.keras.layers.Dense(1024, "relu"),
        tf.keras.layers.Dense(1024, "relu"),
        tf.keras.layers.Dense(num_domains),
    ])
    return feature_extractor, task_classifier, domain_classifier


def make_vada_model(num_classes, num_domains, small=False):
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
            tf.keras.layers.Dense(num_classes),
        ])
    domain_classifier = tf.keras.Sequential([
        tf.keras.layers.Flatten(),

        tf.keras.layers.Dense(100),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.ReLU(),

        tf.keras.layers.Dense(num_domains),
    ])
    return feature_extractor, task_classifier, domain_classifier


@register_model("images_vada_small")
def make_vada_model_small(*args):
    return make_vada_model(*args, small=True)


@register_model("images_vada_large")
def make_vada_model_large(*args):
    return make_vada_model(*args, small=False)


@register_model("images_resnet50")
def make_resnet50_model(num_classes, num_domains):
    """ ResNet50 pre-trained on ImageNet -- for use with Office-31 datasets
    Input should be 224x224x3 """
    feature_extractor = tf.keras.applications.ResNet50(
        include_top=False, pooling="avg")
    task_classifier = tf.keras.Sequential([
        tf.keras.layers.Flatten(),
        tf.keras.layers.Dense(num_classes),
    ])
    domain_classifier = tf.keras.Sequential([
        tf.keras.layers.Flatten(),
        tf.keras.layers.Dense(num_domains),
    ])
    return feature_extractor, task_classifier, domain_classifier


class CnnModelBase(ModelBase):
    """
    Support a variety of CNN-based models, pick via command-line argument
    """
    def __init__(self, num_classes, num_domains, model_name, **kwargs):
        super().__init__(**kwargs)
        self.num_classes = num_classes
        self.num_domains = num_domains
        self.feature_extractor, self.task_classifier, self.domain_classifier \
            = get_model(model_name, num_classes, num_domains)


class BasicModel(CnnModelBase):
    """ Model without adaptation (i.e. no DANN) """
    pass


class DannModelBase:
    """ DANN adds a gradient reversal layer before the domain classifier

    Note: we don't inherit from CnnModelBase or any other specific model because
    we want to support either CnnModelBase, RnnModelBase, etc. with multiple
    inheritance.
    """
    def __init__(self, num_classes, num_domains, global_step,
            total_steps, **kwargs):
        super().__init__(num_classes, num_domains, **kwargs)
        grl_schedule = DannGrlSchedule(total_steps)
        self.flip_gradient = FlipGradient(global_step, grl_schedule)

    def call_domain_classifier(self, fe, task, **kwargs):
        grl_output = self.flip_gradient(fe, **kwargs)
        return self.domain_classifier(grl_output, **kwargs)


class DannModel(DannModelBase, CnnModelBase):
    """ Model with adaptation (i.e. with DANN) """
    pass


class HeterogeneousDannModel(DannModelBase, CnnModelBase):
    """ Heterogeneous DANN model has multiple feature extractors,
    very similar to DannSmoothModel() code except this has multiple FE's
    not multiple DC's """
    def __init__(self, *args, num_feature_extractors, **kwargs):
        super().__init__(*args, **kwargs)

        # Requires multiple feature extractors
        new_feature_extractor = [self.feature_extractor]

        # Start at 1 since we already have one
        for i in range(1, num_feature_extractors):
            new_feature_extractor.append(
                tf.keras.models.clone_model(self.feature_extractor))

        self.feature_extractor = new_feature_extractor

    @property
    def trainable_variables_fe(self):
        # We have multiple feature extractors, so get all variables
        fe_vars = []

        for fe in self.feature_extractor:
            fe_vars += fe.trainable_variables

        return fe_vars

    def call_feature_extractor(self, inputs, which_fe=None, **kwargs):
        # Override so we don't pass which_fe argument to model
        assert which_fe is not None, \
            "must specify which feature extractor to use"
        return self.feature_extractor[which_fe](inputs, **kwargs)

    def call_task_classifier(self, fe, which_fe=None, **kwargs):
        # Override so we don't pass which_fe argument to model
        return self.task_classifier(fe, **kwargs)

    def call_domain_classifier(self, fe, task, which_fe=None, **kwargs):
        # Override so we don't pass which_fe argument to model
        # Copy of the DANN version only with above arg change
        grl_output = self.flip_gradient(fe, **kwargs)
        return self.domain_classifier(grl_output, **kwargs)


class SleepModel(DannModelBase, CnnModelBase):
    """ Sleep model is DANN but concatenating task classifier output (with stop
    gradient) with feature extractor output when fed to the domain classifier """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.concat = tf.keras.layers.Concatenate(axis=1)
        self.stop_gradient = StopGradient()

    def call_domain_classifier(self, fe, task, **kwargs):
        grl_output = self.flip_gradient(fe, **kwargs)
        task_stop_gradient = self.stop_gradient(task)
        domain_input = self.concat([grl_output, task_stop_gradient])
        return self.domain_classifier(domain_input, **kwargs)


class DannSmoothModel(DannModelBase, CnnModelBase):
    """ DANN Smooth model hs multiple domain classifiers,
    very similar to HeterogeneousDannModel() code except this has multiple DC's
    not multiple FE's """
    def __init__(self, *args, num_domain_classifiers, **kwargs):
        # For MDAN Smooth, it's binary classification but we have a separate
        # discriminator for each source-target pair.
        super().__init__(*args, **kwargs)

        # MDAN Smooth requires multiple domain classifiers, one for each source
        # domain. Assumes a single target domain.
        new_domain_classifier = [self.domain_classifier]

        # Start at 1 since we already have one
        for i in range(1, num_domain_classifiers):
            new_domain_classifier.append(
                tf.keras.models.clone_model(self.domain_classifier))

        self.domain_classifier = new_domain_classifier

    @property
    def trainable_variables_domain(self):
        # We have multiple domain classifiers, so get all variables
        domain_vars = []

        for dc in self.domain_classifier:
            domain_vars += dc.trainable_variables

        return domain_vars

    def call_feature_extractor(self, inputs, which_dc=None, **kwargs):
        # Override so we don't pass which_dc argument to model
        return self.feature_extractor(inputs, **kwargs)

    def call_task_classifier(self, fe, which_dc=None, **kwargs):
        # Override so we don't pass which_dc argument to model
        return self.task_classifier(fe, **kwargs)

    def call_domain_classifier(self, fe, task, which_dc=None, **kwargs):
        assert which_dc is not None, \
            "must specify which domain classifier to use with method Smooth"
        grl_output = self.flip_gradient(fe, **kwargs)
        # 0 = source domain 1 with target, 1 = source domain 2 with target, etc.
        return self.domain_classifier[which_dc](grl_output, **kwargs)


class VradaFeatureExtractor(tf.keras.Model):
    """
    Need to get VRNN state, so we can't directly use Sequential since it can't
    return intermediate layer's extra outputs. And, can't use the functional
    API directly since we don't now the input shape.

    Note: only returns state if vrada=True
    """
    def __init__(self, vrada=True, **kwargs):
        super().__init__(**kwargs)
        assert vrada is True or vrada is False
        self.vrada = vrada

        if self.vrada:
            # Use z for predictions in VRADA like in original paper
            self.rnn = VRNN(100, 100, return_z=True, return_sequences=False)
        else:
            self.rnn = tf.keras.layers.LSTM(100, return_sequences=False)

        self.fe = tf.keras.Sequential([
            tf.keras.layers.Flatten(),
            tf.keras.layers.Dense(100),
            tf.keras.layers.Dense(100),
            tf.keras.layers.Dense(100),
        ])

    def call(self, inputs, **kwargs):
        if self.vrada:
            rnn_output, rnn_state = self.rnn(inputs, **kwargs)
        else:
            rnn_output = self.rnn(inputs, **kwargs)
            rnn_state = None

        fe_output = self.fe(rnn_output, **kwargs)

        return fe_output, rnn_state


class RnnModelBase(ModelBase):
    """ RNN-based model - for R-DANN and VRADA """
    def __init__(self, num_classes, num_domains, model_name, vrada, **kwargs):
        # Note: we ignore model_name here and only define one RNN-based model
        super().__init__(**kwargs)
        self.num_classes = num_classes
        self.num_domains = num_domains
        self.feature_extractor = VradaFeatureExtractor(vrada)
        self.task_classifier = tf.keras.Sequential([
            tf.keras.layers.Dense(50),
            tf.keras.layers.Dense(50),
            tf.keras.layers.Dense(50),
            tf.keras.layers.Dense(num_classes),
        ])
        self.domain_classifier = tf.keras.Sequential([
            tf.keras.layers.Dense(50),
            tf.keras.layers.Dense(50),
            tf.keras.layers.Dense(50),
            tf.keras.layers.Dense(num_domains),
        ])

    def call(self, inputs, training=None, **kwargs):
        """ Since our RNN feature extractor returns two values (output and
        RNN state, which we need for the loss) we need to only pass the output
        to the classifiers, i.e. fe[0] rather than fe """
        self.set_learning_phase(training)
        fe = self.call_feature_extractor(inputs, **kwargs)
        task = self.call_task_classifier(fe[0], **kwargs)
        domain = self.call_domain_classifier(fe[0], task, **kwargs)
        return task, domain, fe


class VradaModel(DannModelBase, RnnModelBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, vrada=True, **kwargs)


class RDannModel(DannModelBase, RnnModelBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, vrada=False, **kwargs)
