from openravepy import rotationMatrixFromAxisAngle, Sensor, RaveCreateCollisionChecker, databases, interfaces,  IkParameterization, GeometryType, matrixFromPose, RaveCreateKinBody, planning_error, openravepy_int,  CollisionOptionsStateSaver
from openravepy.misc import SetViewerUserThread

from itertools import count
from transforms import quat_from_axis_angle, trans_from_pose, manip_trans_from_object_trans,  length, normalize, trans_from_point,  trans_from_quat, trans_from_axis_angle, get_active_config, set_active_config

import math
import numpy as np
import sys

MIN_DELTA = 0.01


class Wrapper(object):

    def __init__(self, value):
        self.id = next(self._ids)
        self.value = value

    def __repr__(self):
        return self.__class__.__name__ + '(%d)' % self.id


class Conf(Wrapper):
    _ids = count(0)


class Pose(Wrapper):
    _ids = count(0)


class Grasp(Wrapper):
    _ids = count(0)


class Traj(Wrapper):
    _ids = count(0)


def get_geometries(body):
    return (geometry for link in body.GetLinks() for geometry in link.GetGeometries())


def set_color(body, color):
    for geometry in get_geometries(body):
        geometry.SetDiffuseColor(color)


def set_transparency(body, transparency):
    for geometry in get_geometries(body):
        geometry.SetTransparency(transparency)


def get_box_dimensions(box):
    [link] = box.GetLinks()
    [geometry] = link.GetGeometries()
    assert geometry.GetType() == GeometryType.Box
    return geometry.GetBoxExtents()


def box_body(env, name, dx, dy, dz, color=None, transparency=None):
    body = RaveCreateKinBody(env, '')
    body.InitFromBoxes(
        np.array([[0, 0, .5 * dz, .5 * dx, .5 * dy, .5 * dz]]), draw=True)
    body.SetName(name)
    if color is not None:
        set_color(body, color)
    if transparency is not None:
        set_transparency(body, transparency)
    return body


def mirror_arm_config(robot, config):
    return config * np.array([1 if left_min == right_min else -1 for left_min, right_min in
                              zip(robot.GetDOFLimits(robot.GetManipulator('leftarm').GetArmIndices())[0],
                                  robot.GetDOFLimits(robot.GetManipulator('rightarm').GetArmIndices())[0])])


def set_manipulator_conf(manipulator, values):
    manipulator.GetRobot().SetDOFValues(values, manipulator.GetArmIndices())


def set_base_conf(body, base_values):
    trans = body.GetTransform()
    trans[:3, :3] = rotationMatrixFromAxisAngle(
        np.array([0, 0, base_values[-1]]))
    trans[:2, 3] = base_values[:2]
    body.SetTransform(trans)


def set_gripper(manipulator, values):
    manipulator.GetRobot().SetDOFValues(values, manipulator.GetGripperIndices())


def open_gripper(manipulator):
    _, upper = manipulator.GetRobot().GetDOFLimits(manipulator.GetGripperIndices())
    set_gripper(manipulator, upper)


def close_gripper(manipulator):
    lower, _ = manipulator.GetRobot().GetDOFLimits(manipulator.GetGripperIndices())
    set_gripper(manipulator, lower)


def solve_inverse_kinematics(manipulator, manip_trans):
    robot = manipulator.GetRobot()
    env = robot.GetEnv()
    with robot:
        robot.SetActiveDOFs(manipulator.GetArmIndices())

        config = manipulator.FindIKSolution(manip_trans, 0)
        if config is None:
            return None
        robot.SetDOFValues(config, manipulator.GetArmIndices())
        if env.CheckCollision(robot) or robot.CheckSelfCollision():
            return None
        return config


def manip_from_pose_grasp(pose, grasp):
    return manip_trans_from_object_trans(trans_from_pose(pose), grasp)


def top_grasps(box):
    (w, l, h) = get_box_dimensions(box)
    origin = trans_from_point(0, 0, -h)
    bottom = trans_from_point(0, 0, -h)
    reflect = trans_from_quat(quat_from_axis_angle(0, -math.pi, 0))
    for i in range(4):
        rotate_z = trans_from_axis_angle(0, 0, i * math.pi / 2)
        yield reflect.dot(origin).dot(bottom).dot(rotate_z)


def side_grasps(box, under=True):
    (w, l, h) = get_box_dimensions(box)
    origin = trans_from_point(0, 0, -2 * h)
    for j in range(1 + under):
        swap_xz = trans_from_axis_angle(0, -math.pi / 2 + j * math.pi, 0)
        for i in range(4):
            rotate_z = trans_from_axis_angle(0, 0, i * math.pi / 2)
            yield swap_xz.dot(rotate_z).dot(origin)


def linear_interpolation(body, q1, q2):
    dq = body.SubtractActiveDOFValues(q2, q1)
    steps = np.abs(np.divide(dq, body.GetActiveDOFResolutions())) + 1
    n = int(np.max(steps))
    for i in range(n):
        yield q1 + (1. + i) / n * dq


def extract_config(manipulator, spec, data):
    return spec.ExtractJointValues(data, manipulator.GetRobot(), manipulator.GetArmIndices())


def sample_manipulator_trajectory(manipulator, traj):
    spec = traj.GetConfigurationSpecification()
    waypoints = [extract_config(manipulator, spec, traj.GetWaypoint(i))
                 for i in range(traj.GetNumWaypoints())]
    yield waypoints[0]
    for start, end in zip(waypoints, waypoints[1:]):
        for conf in linear_interpolation(manipulator.GetRobot(), start, end):
            yield conf


def execute_viewer(env, execute):
    if sys.platform == 'darwin':
        SetViewerUserThread(env, 'qtcoin', execute)
    else:
        env.SetViewer('qtcoin')
        execute()


def initialize_openrave(env, manipulator_name, min_delta=MIN_DELTA, collision_checker='ode'):
    env.StopSimulation()
    for sensor in env.GetSensors():
        sensor.Configure(Sensor.ConfigureCommand.PowerOff)
        sensor.Configure(Sensor.ConfigureCommand.RenderDataOff)
        sensor.Configure(Sensor.ConfigureCommand.RenderGeometryOff)
        env.Remove(sensor)
    env.SetCollisionChecker(RaveCreateCollisionChecker(env, collision_checker))
    env.GetCollisionChecker().SetCollisionOptions(0)

    assert len(env.GetRobots()) == 1
    robot = env.GetRobots()[0]
    cd_model = databases.convexdecomposition.ConvexDecompositionModel(robot)
    if not cd_model.load():
        cd_model.autogenerate()
    l_model = databases.linkstatistics.LinkStatisticsModel(robot)
    if not l_model.load():
        l_model.autogenerate()
    l_model.setRobotWeights()
    l_model.setRobotResolutions(xyzdelta=min_delta)

    robot.SetActiveManipulator(manipulator_name)
    manipulator = robot.GetManipulator(manipulator_name)
    robot.SetActiveDOFs(manipulator.GetArmIndices())
    ikmodel = databases.inversekinematics.InverseKinematicsModel(robot=robot, iktype=IkParameterization.Type.Transform6D,
                                                                 forceikfast=True, freeindices=None, freejoints=None, manip=None)
    if not ikmodel.load():
        ikmodel.autogenerate()
    return robot, manipulator


def collision_saver(env, options):
    return CollisionOptionsStateSaver(env.GetCollisionChecker(), options)


def linear_motion_plan(robot, end_config):
    env = robot.GetEnv()
    with robot:

        with collision_saver(env, openravepy_int.CollisionOptions.ActiveDOFs):
            start_config = get_active_config(robot)
            path = [start_config] + \
                list(linear_interpolation(robot, start_config, end_config))
            for conf in path:
                set_active_config(robot, conf)
                if env.CheckCollision(robot):
                    return None
            return path


def manipulator_motion_plan(base_manip, manipulator, goal, step_length=MIN_DELTA, max_iterations=10, max_tries=1):
    with base_manip.robot:
        base_manip.robot.SetActiveManipulator(manipulator)
        base_manip.robot.SetActiveDOFs(manipulator.GetArmIndices())
        with collision_saver(base_manip.robot.GetEnv(), openravepy_int.CollisionOptions.ActiveDOFs):
            try:
                traj = base_manip.MoveManipulator(goal=goal,
                                                  maxiter=max_iterations, execute=False, outputtraj=None, maxtries=max_tries,
                                                  goals=None, steplength=step_length, outputtrajobj=True, jitter=None, releasegil=False)
                return list(sample_manipulator_trajectory(manipulator, traj))
            except planning_error:
                return None


def workspace_motion_plan(base_manip, manipulator, vector, steps=10):
    distance, direction = length(vector), normalize(vector)
    step_length = distance / steps
    with base_manip.robot:
        base_manip.robot.SetActiveManipulator(manipulator)
        base_manip.robot.SetActiveDOFs(manipulator.GetArmIndices())
        with collision_saver(base_manip.robot.GetEnv(), openravepy_int.CollisionOptions.ActiveDOFs):
            try:
                traj = base_manip.MoveHandStraight(direction, minsteps=10 * steps, maxsteps=steps, steplength=step_length,
                                                   ignorefirstcollision=None, starteematrix=None, greedysearch=True, execute=False, outputtraj=None, maxdeviationangle=None,
                                                   planner=None, outputtrajobj=True)
                return list(sample_manipulator_trajectory(manipulator, traj))
            except planning_error:
                return None