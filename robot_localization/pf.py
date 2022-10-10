#!/usr/bin/env python3

""" This is the starter code for the robot localization project """

from cmath import isnan, sin
from functools import partial
import rclpy
from threading import Thread
from rclpy.time import Time
from rclpy.node import Node
from std_msgs.msg import Header
from sensor_msgs.msg import LaserScan, PointCloud2
from geometry_msgs.msg import PoseWithCovarianceStamped, PoseArray, Pose, Point, Quaternion
from rclpy.duration import Duration
import math
import time
import numpy as np
from occupancy_field import OccupancyField
from helper_functions import TFHelper, draw_random_sample, point_cloud
from rclpy.qos import qos_profile_sensor_data
from angle_helpers import quaternion_from_euler
import heapq

## EXPERIMENTAL
import yappi
import sys


class Particle(object):
    """ Represents a hypothesis (particle) of the robot's pose consisting of x,y and theta (yaw)
        Attributes:
            x: the x-coordinate of the hypothesis relative to the map frame
            y: the y-coordinate of the hypothesis relative ot the map frame
            theta: the yaw of the hypothesis relative to the map frame
    """

    def __init__(self, x=0.0, y=0.0, theta=0.0, w=1.0):
        """ Construct a new Particle
            x: the x-coordinate of the hypothesis relative to the map frame
            y: the y-coordinate of the hypothesis relative ot the map frame
            theta: the yaw of KeyboardInterruptthe hypothesis relative to the map frame
            w: the particle weight (the class does not ensure that particle weights are normalized """
        self.theta = theta
        self.x = x
        self.y = y

    def as_pose(self):
        """ A helper function to convert a particle to a geometry_msgs/Pose message """
        q = quaternion_from_euler(0, 0, self.theta)
        return Pose(position=Point(x=self.x, y=self.y, z=0.0),
                    orientation=Quaternion(x=q[0], y=q[1], z=q[2], w=q[3]))


class ParticleFilter(Node):
    """ The class that represents a Particle Filter ROS Node
        Attributes list:
            base_frame: the name of the robot base coordinate frame (should be "base_footprint" for most robots)
            map_frame: the name of the map coordinate frame (should be "map" in most cases)
            odom_frame: the name of the odometry coordinate frame (should be "odom" in most cases)
            scan_topic: the name of the scan topic to listen to (should be "scan" in most cases)
            n_particles: the number of particles in the filter
            d_thresh: the amount of linear movement before triggering a filter update
            a_thresh: the amount of angular movement before triggering a filter update
            pose_listener: a subscriber that listens for new approximate pose estimates (i.e. generated through the rviz GUI)
            particle_pub: a publisher for the particle cloud
            last_scan_timestamp: this is used to keep track of the clock when using bags
            scan_to_process: the scan that our run_loop should process next
            occupancy_field: this helper class allows you to query the map for distance to closest obstacle
            transform_helper: this helps with various transform operations (abstracting away the tf2 module)
            particle_cloud: a list of particles representing a probability distribution over robot poses
            current_odom_xy_theta: the pose of the robot in the odometry frame when the last filter update was performed.
                                   The pose is expressed as a list [x,y,theta] (where theta is the yaw)
            thread: this thread runs your main loop
    """

    def __init__(self):
        super().__init__('pf')
        self.base_frame = "base_footprint"   # the frame of the robot base
        self.map_frame = "map"               # the name of the map coordinate frame
        self.odom_frame = "odom"             # the name of the odometry coordinate frame
        self.scan_topic = "scan"             # the topic where we will get laser scans from

        # self.n_particles = 300          # the number of particles to use
        self.n_particles = 300         # the number of particles to use

        # store particle weights as np array field of pf
        self.weights = np.ones(self.n_particles)

        # the amount of linear movement before performing an update
        self.d_thresh = 0.05
        # self.d_thresh = 0. # DEBUG

        # the amount of angular movement before performing an update
        self.a_thresh = math.pi/6

        self.xy_std = 0.7         # initial cloud x-y   std deviation
        self.th_std = math.pi/7   # initial cloud theta std deviation

        # threshold for evaluating whether projected scan points are valid
        self.scan_eval_threshold = 0.2

        # pose_listener responds to selection of a new approximate robot location (for instance using rviz)
        self.create_subscription(
            PoseWithCovarianceStamped, 'initialpose', self.update_initial_pose, 7)

        # publish the current particle cloud.  This enables viewing particles in rviz.
        self.particle_pub = self.create_publisher(
            PoseArray, "particlecloud", qos_profile_sensor_data)

        # # DEBUG: publish Pointcloud2 from particle
        # self.pcd_pub = self.create_publisher(
        #     PointCloud2, 'scan_projection', 10)

        # laser_subscriber listens for data from the lidar
        self.create_subscription(
            LaserScan, self.scan_topic, self.scan_received, 10)

        # this is used to keep track of the timestamps coming from bag files
        # knowing this information helps us set the timestamp of our map -> odom
        # transform correctly
        self.last_scan_timestamp = None
        # this is the current scan that our run_loop should process
        self.scan_to_process = None
        # your particle cloud will go here
        self.particle_cloud = []

        self.current_odom_xy_theta = []
        self.occupancy_field = OccupancyField(self)
        self.transform_helper = TFHelper(self)

        # we are using a thread to work around single threaded execution bottleneck
        thread = Thread(target=self.loop_wrapper)
        thread.start()

        self.transform_update_timer = self.create_timer(
            0.05, self.pub_latest_transform)

    def pub_latest_transform(self):
        """ This function takes care of sending out the map to odom transform """
        if self.last_scan_timestamp is None:
            return
        postdated_timestamp = Time.from_msg(
            self.last_scan_timestamp) + Duration(seconds=0.1)
        self.transform_helper.send_last_map_to_odom_transform(
            self.map_frame, self.odom_frame, postdated_timestamp)

    def pub_color_scan(self, r, theta):
        """ This method handles publishing a colored pointcloud of the robot's
            scan data, projected onto the particle filter's guess of the 
            robot's pose in the map frame
        """
        # if no robot pose define, return none
        if not hasattr(self, "robot_pose"):
            return

        # project particle's scan data to map frame
        robot_pose = self.transform_helper.convert_pose_to_xy_and_theta(self.robot_pose)
        robot_particle = Particle(robot_pose[0],
                                  robot_pose[1],
                                  robot_pose[2])
        scan_projected = self.project_scan_to_map(r, theta, robot_particle)

        # compute scan point colors based on distance from nearest obstacle:
        point_colors = []
        for point in scan_projected:
            # get point dist to nearest obstacle
            try:
                d = self.occupancy_field.get_closest_obstacle_distance(
                    point[0], point[1])
            except:
                break

            # write color of point
            point_colors.append(d)

        # # publish projected points
        # viz_points = np.array([scan_projected[:, 0], scan_projected[:, 1], np.zeros(
        #     len(theta)), point_colors]).transpose()
        # self.pcd_pub.publish(point_cloud(viz_points, "map"))

    def loop_wrapper(self):
        """ This function takes care of calling the run_loop function repeatedly.
            We are using a separate thread to run the loop_wrapper to work around
            issues with single threaded executors in ROS2 """
        # ## EXPERIMENTAL
        # yappi.start()
        # for i in range(0):
        #     self.run_loop()
        #     time.sleep(0.1)
        # yappi.stop()

        # # filter by module object
        # current_module = sys.modules[__name__]
        # stats = yappi.get_func_stats(
        #     filter_callback=lambda x: yappi.module_matches(x, [current_module])
        # )  # x is a yappi.YFuncStat object
        # stats.sort("name", "desc").print_all()
                
        while True:
            self.run_loop()
            time.sleep(0.1)

    def run_loop(self):
        """ This is the main run_loop of our particle filter.  It checks to see if
            any scans are ready and to be processed and will call several helper
            functions to complete the processing.

            You do not need to modify this function, but it is helpful to understand it.
        """
        if self.scan_to_process is None:
            return
        msg = self.scan_to_process

        (new_pose, delta_t) = self.transform_helper.get_matching_odom_pose(self.odom_frame,
                                                                           self.base_frame,
                                                                           msg.header.stamp)
        if new_pose is None:
            # we were unable to get the pose of the robot corresponding to the scan timestamp
            if delta_t is not None and delta_t < Duration(seconds=0.0):
                # we will never get this transform, since it is before our oldest one
                self.scan_to_process = None
            return

        (r, theta) = self.transform_helper.convert_scan_to_polar_in_robot_frame(
            msg, self.base_frame)
        
        # visualize current laser scan
        self.pub_color_scan(r, theta)

        # clear the current scan so that we can process the next one
        self.scan_to_process = None

        self.odom_pose = new_pose
        new_odom_xy_theta = self.transform_helper.convert_pose_to_xy_and_theta(
            self.odom_pose)
        print("x: {0}, y: {1}, yaw: {2}".format(*new_odom_xy_theta))

        if not self.current_odom_xy_theta:
            self.current_odom_xy_theta = new_odom_xy_theta
        elif not self.particle_cloud:
            # now that we have all of the necessary transforms we can update the particle cloud
            self.initialize_particle_cloud(msg.header.stamp)
        elif self.moved_far_enough_to_update(new_odom_xy_theta):
        # else:
            # we have moved far enough to do an update!
            self.update_particles_with_odom()    # update based on odometry
            self.update_particles_with_laser(
                r, theta)                        # update based on laser scan
            self.update_robot_pose()             # update robot's pose based on particles

            # resample particles to focus on areas of high density
            self.resample_particles()
        # publish particles (so things like rviz can see them)
        self.publish_particles(msg.header.stamp)

    def moved_far_enough_to_update(self, new_odom_xy_theta):
        return math.fabs(new_odom_xy_theta[0] - self.current_odom_xy_theta[0]) > self.d_thresh or \
            math.fabs(new_odom_xy_theta[1] - self.current_odom_xy_theta[1]) > self.d_thresh or \
            math.fabs(
                new_odom_xy_theta[2] - self.current_odom_xy_theta[2]) > self.a_thresh

    def update_robot_pose(self):
        """ Update the estimate of the robot's pose given the updated particles.
            There are two logical methods for this:
                (1): compute the mean pose
                (2): compute the most likely pose (i.e. the mode of the distribution)
        """
        # first make sure that the particle weights are normalized
        self.normalize_particles()
        
        # # select and average the best n particles
        # n = 20
        # best_particles = self.n_highest_weighted(n)
        # robot_pose = Particle(np.mean([p.x     for p in best_particles]),
        #                       np.mean([p.y     for p in best_particles]),
        #                       np.mean([p.theta for p in best_particles])).as_pose()

        # TODO: average unit vecs to calculate angle mean
        # select the best n particles
        n = self.n_particles // 5
        best_particles = self.n_highest_weighted(n)

        # make np array out of best particles
        best_particles = np.array([[p.x, p.y, p.theta] for p in best_particles])
        # compute unit vector columns
        unit_vecs = np.array([np.cos(best_particles[:,2]), np.sin(best_particles[:,2])]).transpose()
        # stick unit vector columns into matrix of best particles
        best_particles_uv = np.append(best_particles[:,0:2], unit_vecs, 1)

        # average matrix down the columns
        average_particle = np.average(best_particles_uv, 0)

        # get angle from average unit vec's compnents
        average_angle = np.arctan2(average_particle[3], average_particle[2])

        self.robot_pose = Particle(average_particle[0], average_particle[1], average_angle).as_pose()


        # # TODO: weighted average instead of average of highest weighted
        

        # # assign best particle's pose to robot_pose
        # self.robot_pose = robot_pose
        print(self.robot_pose) # DEBUG

        self.transform_helper.fix_map_to_odom_transform(self.robot_pose,
                                                        self.odom_pose)

    def update_particles_with_odom(self):
        """ Update the particles using the newly given odometry pose.
            The function computes the value delta which is a tuple (x,y,theta)
            that indicates the change in position and angle between the odometry
            when the particles were last updated and the current odometry.
        """
        new_odom_xy_theta = self.transform_helper.convert_pose_to_xy_and_theta(
            self.odom_pose)
        # compute the change in x,y,theta since our last update
        if self.current_odom_xy_theta:
            old_odom_xy_theta = self.current_odom_xy_theta
            delta = (new_odom_xy_theta[0] - self.current_odom_xy_theta[0],
                     new_odom_xy_theta[1] - self.current_odom_xy_theta[1],
                     new_odom_xy_theta[2] - self.current_odom_xy_theta[2])

            self.current_odom_xy_theta = new_odom_xy_theta
        else:
            self.current_odom_xy_theta = new_odom_xy_theta
            return

        for p in self.particle_cloud:
            p.x += (delta[0] + np.random.normal(scale=0.03))
            p.y += (delta[1] + np.random.normal(scale=0.03))
            p.theta += (delta[2] + np.random.normal(scale=0.3))
        
        # TODO: modify noise?

    def resample_particles(self):
        """ Resample the particles according to the new particle weights.
            The weights stored with each particle should define the probability that a particular
            particle is selected in the resampling step.  You may want to make use of the given helper
            function draw_random_sample in helper_functions.py.
        """
        # make sure the distribution is normalized
        self.normalize_particles()
        
        # resample with helper function
        self.particle_cloud = draw_random_sample(self.particle_cloud, self.weights, self.n_particles)

    
    def n_highest_weighted(self, n):
        """ Returns list of the n particles with highest weights

            Args:
                n (int): number of particles
        """
        # compute indices of highest weighted particles
        idx_best_particles = heapq.nlargest(n, range(len(self.weights)), key=lambda x: self.weights[x])
        # slice particle cloud by indices and return
        return [self.particle_cloud[i] for i in idx_best_particles]

    def update_particles_with_laser(self, r, theta):
        """ Updates the particle weights in response to the scan data
            r: the distance readings to obstacles
            theta: the angle relative to the robot frame for each corresponding reading 
        """
        # shitty for loop method:
        for idx, p in enumerate(self.particle_cloud):
            # project particle's scan data to map frame
            scan_projected = self.project_scan_to_map(r, theta, p)

            # evaluate raw scan weight (0-n_particles scale) using thresholding method:
            raw_weight = 0
            ds = self.occupancy_field.get_closest_obstacle_distance(
                scan_projected[:,0], scan_projected[:,1])
            raw_weight = sum(ds < self.scan_eval_threshold)

            # assign raw weight to particle
            self.weights[idx] = raw_weight

    def project_scan_to_map(self, rs, thetas, p):
        """
        Returns the x and y coordinates of a laser scan, translated as if it
        originated from a particle
        Args:
            rs (list of floats): r coordinates of scan in robot frame
            thetas (list of floats): theta coords of scan in robot frame
            p (Particle): particle to project from
        Return:
            (nx2 float np array): the projected scan
        """
        # scan_polar = np.array([rs, thetas])
        thetas = np.array(thetas) + p.theta # rotate thetas
        scan_projected = np.array([np.cos(thetas), np.sin(thetas)])*rs+[[p.x], [p.y]]
        return scan_projected.transpose()

    def update_initial_pose(self, msg):
        """ Callback function to handle re-initializing the particle filter based on a pose estimate.
            These pose estimates could be generated by another ROS Node or could come from the rviz GUI """
        xy_theta = self.transform_helper.convert_pose_to_xy_and_theta(
            msg.pose.pose)
        self.initialize_particle_cloud(msg.header.stamp, xy_theta)

    def initialize_particle_cloud(self, timestamp, xy_theta=None):
        """ Initialize the particle cloud.
            Arguments
            xy_theta: a triple consisting of the mean x, y, and theta (yaw) to initialize the
                      particle cloud around.  If this input is omitted, the odometry will be used """
        if xy_theta is None:
            xy_theta = self.transform_helper.convert_pose_to_xy_and_theta(
                self.odom_pose)
        self.particle_cloud = []

        # 1. get normal distributions for x, y, and theta
        x_dist = np.random.normal(xy_theta[0], self.xy_std, self.n_particles)
        y_dist = np.random.normal(xy_theta[1], self.xy_std, self.n_particles)
        th_dist = np.random.normal(xy_theta[2], self.th_std, self.n_particles)

        # 2. build particle list from the x, y, and theta distributions
        self.particle_cloud = [
            Particle(x_dist[i], y_dist[i], th_dist[i], 1) for i in range(self.n_particles)]

        self.normalize_particles()

    def normalize_particles(self):
        """ Make sure the particle weights define a valid distribution (i.e. sum to 1.0) """
        # divide weights elementwise by sum of all weights
        self.weights /= sum(self.weights)

    def publish_particles(self, timestamp):
        particles_conv = []
        for p in self.particle_cloud:
            particles_conv.append(p.as_pose())
        # actually send the message so that we can view it in rviz
        self.particle_pub.publish(PoseArray(header=Header(stamp=timestamp,
                                            frame_id=self.map_frame),
                                  poses=particles_conv))

    def scan_received(self, msg):
        self.last_scan_timestamp = msg.header.stamp
        # we throw away scans until we are done processing the previous scan
        # self.scan_to_process is set to None in the run_loop
        if self.scan_to_process is None:
            self.scan_to_process = msg


def main(args=None):
    rclpy.init()
    n = ParticleFilter()
    rclpy.spin(n)
    rclpy.shutdown()


if __name__ == '__main__':
    main()
