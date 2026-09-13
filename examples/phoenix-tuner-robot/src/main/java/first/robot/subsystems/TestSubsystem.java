// Copyright (c) FIRST and other WPILib contributors.
// Open Source Software; you can modify and/or share it under the terms of
// the WPILib BSD license file in the root directory of this project.

package first.robot.subsystems;

import com.ctre.phoenix6.CANBus;
import com.ctre.phoenix6.configs.MotorOutputConfigs;
import com.ctre.phoenix6.controls.CoastOut;
import com.ctre.phoenix6.controls.DutyCycleOut;
import com.ctre.phoenix6.hardware.TalonFXS;
import com.ctre.phoenix6.signals.NeutralModeValue;
import org.wpilib.command2.Command;
import org.wpilib.command2.SubsystemBase;

/**
 * One Talon FXS (CAN ID 2 on can_s0, the first USB-CAN adapter). Constructing it is what starts
 * the Phoenix diagnostic server for Tuner X. While the robot is enabled the default command keeps
 * the motor in coast-out (bridge disabled, shaft spins freely); the neutral mode is set to Coast
 * too, so it also coasts when disabled.
 *
 * <p>Only the neutral mode is written to the device — the motor arrangement, limits and inversion
 * configured in Tuner X are left alone (a full TalonFXSConfiguration would reset the arrangement
 * to Disabled).
 */
public class TestSubsystem extends SubsystemBase {
  private static final int TALON_ID = 2;
  private static final int CAN_BUS = 0; // can_s0

  private final TalonFXS motor = new TalonFXS(TALON_ID, CANBus.systemcore(CAN_BUS));
  private final CoastOut coastOut = new CoastOut();
  private final DutyCycleOut dutyCycle = new DutyCycleOut(0);

  public TestSubsystem() {
    var motorOutput = new MotorOutputConfigs();
    motor.getConfigurator().refresh(motorOutput);
    motorOutput.NeutralMode = NeutralModeValue.Coast;
    motor.getConfigurator().apply(motorOutput);
  }

  /** Coast-out control request, sent every loop while enabled (the default command). */
  public Command coast() {
    return run(() -> motor.setControl(coastOut)).withName("Coast");
  }

  /** Runs the motor at a fixed duty cycle; unused by default, handy for bench tests. */
  public Command runAt(double dutyCycleOutput) {
    return run(() -> motor.setControl(dutyCycle.withOutput(dutyCycleOutput))).withName("RunAt");
  }
}
