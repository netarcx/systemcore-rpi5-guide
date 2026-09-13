// Copyright (c) FIRST and other WPILib contributors.
// Open Source Software; you can modify and/or share it under the terms of
// the WPILib BSD license file in the root directory of this project.

package first;

import org.wpilib.framework.RobotBase;

/** Entry point. WPILib 2027 Alpha 7 takes a factory here (Robot::new). */
public final class Main {
  private Main() {}

  public static void main(String... args) {
    RobotBase.startRobot(first.robot.Robot::new);
  }
}
