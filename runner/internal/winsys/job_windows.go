package winsys

import (
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
)

var windowsSysProcAttr = syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000} // CREATE_NO_WINDOW

// Job is a kill-on-close Job Object: when its last handle closes (the service process exits, however
// it exits) every process in it is terminated, run-servers and browsers included.
type Job struct{ h windows.Handle }

func NewKillOnCloseJob() (*Job, error) {
	h, err := windows.CreateJobObject(nil, nil)
	if err != nil {
		return nil, err
	}
	var info windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION
	info.BasicLimitInformation.LimitFlags = windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
	if _, err := windows.SetInformationJobObject(h, windows.JobObjectExtendedLimitInformation,
		uintptr(unsafe.Pointer(&info)), uint32(unsafe.Sizeof(info))); err != nil {
		windows.CloseHandle(h)
		return nil, err
	}
	return &Job{h: h}, nil
}

// AssignCurrent puts this process in the job; its children inherit it.
func (j *Job) AssignCurrent() error {
	return windows.AssignProcessToJobObject(j.h, windows.CurrentProcess())
}

// Assign puts another process in the job.
func (j *Job) Assign(pid int) error {
	h, err := windows.OpenProcess(windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE, false, uint32(pid))
	if err != nil {
		return err
	}
	defer windows.CloseHandle(h)
	return windows.AssignProcessToJobObject(j.h, h)
}

// Terminate ends every process in the job.
func (j *Job) Terminate(exit uint32) error { return windows.TerminateJobObject(j.h, exit) }

// Close closes the handle; with kill-on-close that ends the job's processes once no handle is left.
func (j *Job) Close() error { return windows.CloseHandle(j.h) }
