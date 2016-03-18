#!/usr/bin/env python
#
# This code is a part of `ardrone_autopilot` project
# which is distributed under the MIT license.
# See `LICENSE` file for details.
#
"""Target recognition and visualization


Inputs
------

* /in/image -- main picture stream.
* /in/info -- camera info stream.


Outputs
-------

* /out/image -- result image.


Parameters
----------

* ~encoding = "bgr8" [str] -- video encoding used by bridge.
* ~path = "../data/target.jpg" [str] -- path to the target sample
  (relative to the file location).

"""

import cv2
from cv_bridge import CvBridge, CvBridgeError

import numpy as np

import rospy

from sensor_msgs.msg import Image, CameraInfo

from os.path import dirname, join
from operator import attrgetter
from collections import deque


class MovingAverage(object):
    def __init__(self, coeffs=None):
        if coeffs is None:
            coeffs = [1] * 5

        self.coeffs = coeffs
        self.items = deque(maxlen=len(coeffs))

    def __call__(self, new_val):
        self.items.append(new_val)
        return sum(c * v for c, v in zip(self.coeffs, self.items))


class Detect(object):
    def __init__(self, kp=None, desc=None):
        """Keypoints and descriptors of an image"""
        self.kp = kp
        self.desc = desc


class ControllerNode(object):
    def __init__(self):
        self.pub = rospy.Publisher('/out/image', Image, queue_size=5)

        self.bridge = CvBridge()
        self.encoding = rospy.get_param('~encoding', 'bgr8')

        self.info = None

        rospy.Subscriber('/in/info', CameraInfo, self.on_info, queue_size=1)
        rospy.Subscriber('/in/image', Image, self.on_image, queue_size=1)

        path = join(dirname(dirname(__file__)), 'data/target.jpg')
        path = rospy.get_param('~path', path)

        self.detector = cv2.ORB()
        self.pattern = cv2.imread(path, 0)
        self.pattern_detect = self.detect(self.pattern)

        self.matcher = cv2.FlannBasedMatcher(
            dict(
                algorithm=6,
                table_number=6,
                key_size=12,
                multi_probe_level=1
            ),
            dict(
                checks=100
            )
        )

        h, w = self.pattern.shape

        self.target_points_2d = np.float32(
            [[0, 0], [0, h-1], [w-1, h-1], [w-1, 0]]).reshape(-1, 1, 2)

        h_, w_ = h // 2, w // 2

        self.target_points_3d = np.float32(
            [[-w_+1, -h_+1, 0],
             [-w_+1, h_-1, 0],
             [w_-1, h_-1, 0],
             [w_-1, -h_+1, 0]])

        self.x_ratio = 0.7
        self.y_ratio = 0.7

        self.match_test_ratio = 0.7

        self.knn_parameter = 2

    def on_info(self, data):
        self.info = data

    def on_image(self, data):
        if self.info is None:
            return

        img = self.bridge.imgmsg_to_cv2(data, self.encoding)
        img_out = self.process_image(img)

        img_msg = self.bridge.cv2_to_imgmsg(img_out, self.encoding)
        self.pub.publish(img_msg)

    def process_image(self, img):
        if self.info is None:
            return

        image_detect = self.detect(img)

        if len(image_detect.kp) < 15:
            return img

        matches = self.match(image_detect)
        matched_image_detect = self.filter_train_kp(
            image_detect, matches)
        matched_pattern_detect = self.filter_query_kp(
            self.pattern_detect, matches)

        if len(matches) < 15:
            return self.draw_match(img, image_detect, matched_image_detect,
                                   success=False)

        transform, mask = cv2.findHomography(
            np.float32(map(attrgetter('pt'), matched_pattern_detect.kp)),
            np.float32(map(attrgetter('pt'), matched_image_detect.kp)),
            cv2.RANSAC, 5.0)

        bbox = cv2.perspectiveTransform(self.target_points_2d, transform)

        if not self.check_match(bbox):
            return self.draw_match(img, image_detect, matched_image_detect,
                                   bbox, success=False)

        camera_matrix = np.float32(self.info.K).reshape(3, 3)
        camera_distortion = np.float32(self.info.D)

        # TODO: use previously received data
        rot, trans, inl = cv2.solvePnPRansac(
            self.target_points_3d, np.float32(bbox),
            camera_matrix, camera_distortion,
            iterationsCount=10,
            flags=cv2.ITERATIVE,
            reprojectionError=10
        )

        return self.draw_match(img, image_detect, matched_image_detect, bbox,
                               success=True)

    def detect(self, img, *args, **kwargs):
        """Detects keypoints and descriptors of the image

        Returns a `Detect` class instance.

        """
        kp, desc = self.detector.detectAndCompute(img, None, *args, **kwargs)
        return Detect(kp, desc)

    def match(self, image_detect):
        """Matches the detected features against the pattern features"""
        matches = self.matcher.knnMatch(self.pattern_detect.desc,
                                        image_detect.desc,
                                        k=self.knn_parameter)
        good_matches = [i for i in matches if len(i) == 2]
        ratio_matches = [m for m, n in good_matches
                         if m.distance < self.match_test_ratio * n.distance]
        return ratio_matches

    def draw_match(self, img, image_detect, good_image_detect,
                   bbox=None, success=True):
        """Draws the found and matched keypoints and the target rectangle"""
        if success:
            color = (0, 255, 0)
        else:
            color = (0, 0, 255)

        img = cv2.drawKeypoints(img, image_detect.kp,
                                color=(0, 20, 20), flags=0)
        img = cv2.drawKeypoints(img, good_image_detect.kp,
                                color=color, flags=0)

        if bbox is not None:
            cv2.polylines(img, [np.int32(bbox)], True, color, 3)

        return img

    def check_match(self, points):
        """Returns True if the given vertexes for a valid rectandle"""
        if len(points) != 4:
            return False

        a = self.distance(points[0][0], points[1][0])
        b = self.distance(points[1][0], points[2][0])
        c = self.distance(points[2][0], points[3][0])
        d = self.distance(points[3][0], points[0][0])

        if (a / c < self.x_ratio or c / a < self.x_ratio or
                b / d < self.y_ratio or d / b < self.y_ratio):
            return False

        return True

    @staticmethod
    def filter_query_kp(query_detect, matches):
        """Filters the matched `query_detect` keypoints

        Returns a `Detect` class instance with filtered keypoints iterable
        and no descriptors.

        """
        return Detect([query_detect.kp[m.queryIdx] for m in matches])

    @staticmethod
    def filter_train_kp(train_detect, matches):
        """Filters the matched `train_detect` keypoints

        Returns a `Detect` class instance with filtered keypoints iterable
        and no descriptors.

        """
        return Detect([train_detect.kp[m.trainIdx] for m in matches])

    @staticmethod
    def distance(a, b):
        return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


if __name__ == "__main__":
    rospy.init_node('ui_node')

    rospy.loginfo('Starting autopilot node')

    node = ControllerNode()

    try:
        rospy.spin()
    except KeyboardInterrupt:
        pass
