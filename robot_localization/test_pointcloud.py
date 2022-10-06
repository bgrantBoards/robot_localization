import rclpy 
from rclpy.node import Node
import sensor_msgs.msg as sensor_msgs
import std_msgs.msg as std_msgs
from helper_functions import point_cloud

import numpy as np

class PCPublisher(Node):
    def __init__(self, points):
        super().__init__('bens_pcd_publisher')

        print("testing")
        self.points = points

        self.pcd_pub = self.create_publisher(sensor_msgs.PointCloud2, 'bens_cloud', 10)

        timer_period = 1/30.0
        self.timer = self.create_timer(timer_period, self.timer_callback)
    
    def timer_callback(self):
        # Here I use the point_cloud() function to convert the numpy array 
        # into a sensor_msgs.PointCloud2 object. The second argument is the 
        # name of the frame the point cloud will be represented in. The default
        # (fixed) frame in RViz is called 'map'
        self.pcd = point_cloud(self.points, 'map')
        # Then I publish the PointCloud2 object 
        self.pcd_pub.publish(self.pcd)

my_points = []
for i in range(255):
    my_points.append([3*i/255, 3*i/255, 3*i/255, i])
my_points = np.array(my_points)

def main(args=None):
    # Boilerplate code.
    rclpy.init(args=args)
    pc_pub = PCPublisher(my_points)
    rclpy.spin(pc_pub)
    
    # Destroy the node explicitly
    # (optional - otherwise it will be done automatically
    # when the garbage collector destroys the node object)
    pc_pub.destroy_node()
    rclpy.shutdown()

if __name__ == "__main__":
    main()