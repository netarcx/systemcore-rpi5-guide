// Copyright (c) FIRST and other WPILib contributors.
// Open Source Software; you can modify and/or share it under the terms of
// the WPILib BSD license file in the root directory of this project.

package first.robot;

import first.robot.subsystems.TestSubsystem;
import org.wpilib.command2.Command;
import org.wpilib.command2.Commands;

/**
 * Minimal container: one subsystem that owns one Phoenix device. Constructing that device is what
 * starts the Phoenix diagnostic server (port 1250) that Tuner X connects to. No controller bindings
 * and no autonomous, so there is nothing to warn about when no Driver Station is attached.
 */
public class RobotContainer {
  private final TestSubsystem testSubsystem = new TestSubsystem();

  public RobotContainer() {
    testSubsystem.setDefaultCommand(testSubsystem.stopMotors());
  }

  public Command getAutonomousCommand() {
    return Commands.none();
  }
}
